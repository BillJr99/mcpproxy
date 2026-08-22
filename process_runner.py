"""
process_runner.py — Spawn and talk to any stdio-based MCP server subprocess.

Supports npx, uvx, pip-installed commands, npm-installed binaries, or any other
command that speaks the MCP stdio transport (one JSON-RPC object per line on
stdout, stdin for requests).

Each provider YAML that has a ``package:`` block (instead of a ``code:`` block)
is handled here.

Two use-cases
─────────────
1. Introspection (frontend wizard): spawn → initialize → tools/list → kill.
2. Tool calls (server): one persistent session per command string;
   process is (re-)started on demand and reused across calls.
"""

import asyncio
import json
import os
import re
import shlex
import threading
import traceback
from typing import Any
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# OAuth-bridge (mcp-remote) support
# ---------------------------------------------------------------------------
#
# Remote, OAuth-protected MCP servers (e.g. the official Asana server at
# https://mcp.asana.com/v2/mcp) are reached through the community `mcp-remote`
# bridge, spawned exactly like any other stdio package provider.  On first run —
# or whenever the cached refresh token has expired or been revoked — mcp-remote
# prints an authorization URL to *stderr* and blocks the MCP `initialize`
# handshake until the user completes the browser OAuth flow.
#
# We scrape that URL out of stderr so the UI can surface a clickable
# "Authorize" link, and we give the handshake a longer, configurable timeout
# so a human has time to finish authorizing.  Once a valid token cache exists
# mcp-remote refreshes silently and none of this is exercised.

# How long (seconds) to wait for the `initialize` response.  Generous by
# default so a first-time interactive OAuth flow can complete; override with
# MCPPROXY_AUTH_INIT_TIMEOUT.
AUTH_INIT_TIMEOUT = float(os.environ.get("MCPPROXY_AUTH_INIT_TIMEOUT", "300"))

# asyncio caps a StreamReader line at 64 KiB.  One MCP JSON-RPC message is one
# line, and an initialize reply or a large tools/list routinely exceeds that —
# readline() then raises ValueError("...chunk is longer than limit") and the
# server is unusable through the proxy.  Raise the ceiling instead.
STREAM_LIMIT = int(os.environ.get("MCPPROXY_STREAM_LIMIT", str(16 * 1024 * 1024)))

# Latest pending authorization URL per spawn command, populated from stderr.
# The UI (same process — the frontend runs as a daemon thread inside the MCP
# server) polls this so it can show the link while a spawn is blocked on auth.
pending_auth_urls: dict[str, str] = {}

# Loopback callback port mcp-remote reported for each spawn command, scraped
# from stderr.  This is what the manual-callback replay aims at: when the
# provider YAML omits the port argument mcp-remote picks one at random and only
# ever announces it here.  Deliberately *not* cleared when a flow completes —
# the port is a durable fact about the command, and whether anything is
# actually listening is answered by a TCP probe, not by membership in this dict.
callback_listener_ports: dict[str, int] = {}

# Commands whose MCP initialize handshake has completed successfully in this
# process. Provider-status APIs use this to distinguish dependency setup from
# a live authenticated remote bridge. No tokens or authorization URLs live in
# this set.
authenticated_commands: set[str] = set()

# Why a command's bridge last failed to start, in words a user can act on.
# A crashed bridge otherwise leaves no trace in the UI at all: setup succeeded
# (handlers are just closures), so the provider reports "ready" and the real
# cause reaches only the server's stdout.
bridge_errors: dict[str, str] = {}

# Commands with a spawn in flight, guarded by a plain threading.Lock because
# the startup warm-up runs asyncio.run on its own thread while the UI runs a
# different loop.  Two bridges for one remote server share mcp-remote's on-disk
# PKCE verifier and overwrite each other, so the callback for the first flow
# then fails with "code_verifier does not match".
_spawning: set[str] = set()
_spawn_lock = threading.Lock()

_URL_RE = re.compile(r"https?://[^\s'\"<>]+")
# Lines that hint mcp-remote (or a similar bridge) is asking the user to
# authorize.  Matched case-insensitively against each stderr line.
_AUTH_HINT_RE = re.compile(
    r"authoriz|oauth|visit (?:this|the following)|open (?:this|the following)",
    re.IGNORECASE,
)
# "Using specified callback port: 8887" / "Using automatically selected
# callback port: 3334" — mcp-remote announces the loopback listener it is about
# to bind.  Matched on its own, without _AUTH_HINT_RE, because the line need not
# mention authorization at all.
_CALLBACK_PORT_RE = re.compile(r"callback (?:server )?port[:\s]+(\d{1,5})", re.IGNORECASE)
# Lines that satisfy _AUTH_HINT_RE but are not an invitation to visit anything:
# OAuth discovery output ("Discovered authorization server: <issuer>"), warnings,
# and error dumps carrying an errorUri.  Without this the issuer *base* URL gets
# published as the pending authorization URL and the UI offers a dead link.
_AUTH_NOISE_RE = re.compile(
    r"discover(?:ing|ed)|authorization server|error|warning|fatal", re.IGNORECASE
)


# mcp-remote failures a user can actually do something about.  Each entry maps
# a stderr pattern to a sentence naming the remedy; anything unrecognised falls
# back to the last stderr line, so something useful always surfaces.
_FAILURE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"Environment variable '([^']+)' not found for header '([^']+)'"),
        "{0} is not set in the bridge's environment, so the {1} header was sent "
        "empty. Add it to .env and restart, or declare it under the provider's "
        "package.env_keys so it is picked up on the next spawn.",
    ),
    (
        re.compile(r"does not support dynamic client registration", re.IGNORECASE),
        "This server does not support dynamic client registration, so mcp-remote "
        "cannot register itself. Supply a pre-registered client with "
        "--static-oauth-client-info @<file>, or authenticate with a token using "
        "--header instead of OAuth.",
    ),
    (
        re.compile(r"code_verifier does not match", re.IGNORECASE),
        "The callback belonged to a different authorization attempt. Start a "
        "fresh authorization and use the link it produces.",
    ),
    (
        re.compile(r"invalid_client|InvalidClientError", re.IGNORECASE),
        "The OAuth client credentials were rejected. Check the client id and "
        "secret in the file passed to --static-oauth-client-info.",
    ),
)


def _classify_failure(lines: list[str]) -> str | None:
    """Return an actionable explanation for a bridge failure, if one is known.

    Patterns are tried in table order, not log order, because the last error a
    bridge prints is usually downstream of the real cause.  A missing header
    variable, for instance, makes mcp-remote fall back to OAuth and *then* fail
    on dynamic client registration — reporting that second error would send the
    user off to register a client when all they need is to set the variable.
    """
    for pattern, template in _FAILURE_PATTERNS:
        for line in lines:
            m = pattern.search(line)
            if m:
                return template.format(*m.groups())
    return None


def _extract_auth_url(line: str) -> str | None:
    """Return an authorization URL from *line* if it looks like an auth prompt."""
    if not _AUTH_HINT_RE.search(line) or _AUTH_NOISE_RE.search(line):
        return None
    m = _URL_RE.search(line)
    return m.group(0) if m else None


def _valid_port(value: int | None) -> int | None:
    return value if value is not None and 1 <= value <= 65535 else None


def _extract_callback_port(line: str) -> int | None:
    """Return the loopback callback port mcp-remote announced on *line*."""
    m = _CALLBACK_PORT_RE.search(line)
    if m:
        return _valid_port(int(m.group(1)))
    # Fall back to the port of any loopback URL on the line, e.g.
    # "OAuth callback server listening at http://127.0.0.1:8887".
    m = _URL_RE.search(line)
    if not m or not _is_loopback_url(m.group(0)):
        return None
    try:
        return _valid_port(urlparse(m.group(0)).port)
    except ValueError:
        return None


def _is_loopback_url(url: str) -> bool:
    """Return whether *url* points at a local callback listener."""
    try:
        return (urlparse(url).hostname or "").lower() in {
            "127.0.0.1", "localhost", "::1",
        }
    except ValueError:
        return False


def _auth_timeout_from_command(parts: list[str]) -> float | None:
    """Return mcp-remote's ``--auth-timeout`` (seconds) if the command sets one."""
    for i, token in enumerate(parts):
        if token == "--auth-timeout" and i + 1 < len(parts):
            raw = parts[i + 1]
        elif token.startswith("--auth-timeout="):
            raw = token.split("=", 1)[1]
        else:
            continue
        try:
            seconds = float(raw)
        except ValueError:
            return None
        # An absurd value is a typo, not an instruction to wait a week.
        return seconds if 0 < seconds <= 86400 else None
    return None


class ProcessSession:
    """A long-lived connection to a single stdio MCP server process."""

    def __init__(
        self,
        command: str,
        cwd: str | None = None,
        env_keys: list[str] | None = None,
    ) -> None:
        self.command = command
        self.cwd = cwd
        self.env_keys = list(env_keys or [])
        self._parts: list[str] = shlex.split(command)
        self._proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._next_id = 0
        # stderr is consumed by a background reader (see _consume_stderr) so we
        # can scrape OAuth authorization URLs in real time; the reader keeps a
        # bounded tail buffer that _drain_stderr_tail reports on failure.
        self._stderr_tail: list[str] = []
        self._stderr_task: asyncio.Task | None = None
        # Authorization URL most recently printed by the subprocess, if any.
        self.pending_auth_url: str | None = None
        # mcp-remote holds the handshake open for its own --auth-timeout.
        # Abandoning initialize before then kills the OAuth callback listener
        # out from under a user who is still authorizing — and out from under
        # the manual-callback paste flow, which replays into that listener.
        declared = _auth_timeout_from_command(self._parts)
        self.init_timeout = (
            max(AUTH_INIT_TIMEOUT, declared + 30) if declared else AUTH_INIT_TIMEOUT
        )

    # ── internal ──────────────────────────────────────────────────────────────

    def _new_id(self) -> int:
        self._next_id += 1
        return self._next_id

    async def _send(self, msg: dict[str, Any]) -> None:
        assert self._proc and self._proc.stdin
        data = json.dumps(msg, separators=(",", ":")) + "\n"
        try:
            self._proc.stdin.write(data.encode())
            await self._proc.stdin.drain()
        except (RuntimeError, BrokenPipeError, ConnectionResetError) as exc:
            # The subprocess died before we could write — a bridge that fails
            # fatally during startup loses its stdin between spawn and the
            # initialize request.  The transport's own message names an internal
            # uvloop handle and explains nothing, so report the real cause.
            stderr_tail = await self._drain_stderr_tail()
            suffix = f"\nsubprocess stderr (tail): {stderr_tail}" if stderr_tail else ""
            raise EOFError(
                f"MCP process exited before it could be initialized{suffix}"
            ) from None

    async def _recv(self, timeout: float = 30.0) -> dict[str, Any]:
        assert self._proc and self._proc.stdout
        try:
            line = await asyncio.wait_for(self._proc.stdout.readline(), timeout=timeout)
        except ValueError as exc:
            # readline() raises this when one line exceeds the stream limit.
            # asyncio's wording ("chunk is longer than limit") names no remedy.
            raise RuntimeError(
                f"MCP server sent a message longer than {STREAM_LIMIT} bytes "
                f"({exc}). Raise MCPPROXY_STREAM_LIMIT."
            ) from None
        if not line:
            # The subprocess closed stdout — usually means it crashed.  Drain
            # stderr (best-effort, non-blocking) so the caller sees the actual
            # cause rather than a bare "closed stdout".
            stderr_tail = await self._drain_stderr_tail()
            suffix = f"\nsubprocess stderr (tail): {stderr_tail}" if stderr_tail else ""
            raise EOFError(f"MCP process closed stdout{suffix}")
        return json.loads(line)

    async def _consume_stderr(self) -> None:
        """Continuously read subprocess stderr.

        Keeps a bounded tail (for crash diagnostics) and scrapes any OAuth
        authorization URL so the UI can surface a clickable "Authorize" link
        while the spawn is blocked on the user completing the browser flow.
        """
        assert self._proc and self._proc.stderr
        try:
            while True:
                raw = await self._proc.stderr.readline()
                if not raw:
                    break
                line = raw.decode(errors="replace").rstrip("\n")
                self._stderr_tail.append(line)
                if len(self._stderr_tail) > 50:
                    del self._stderr_tail[:-50]
                port = _extract_callback_port(line)
                if port is not None:
                    callback_listener_ports[self.command] = port
                url = _extract_auth_url(line)
                if url:
                    # mcp-remote prints both the provider authorization URL and
                    # its localhost callback-listener URL. Never let the latter
                    # replace the external URL the user actually needs to open.
                    if (
                        self.pending_auth_url
                        and not _is_loopback_url(self.pending_auth_url)
                        and _is_loopback_url(url)
                    ):
                        continue
                    self.pending_auth_url = url
                    pending_auth_urls[self.command] = url
                    print(
                        f"[mcpproxy] authorization required for "
                        f"'{self.command}' — authorization URL captured",
                        flush=True,
                    )
        except Exception:
            traceback.print_exc()

    def _start_stderr_reader(self) -> None:
        if self._stderr_task is None or self._stderr_task.done():
            self._stderr_task = asyncio.ensure_future(self._consume_stderr())

    def _clear_pending_auth(self) -> None:
        self.pending_auth_url = None
        pending_auth_urls.pop(self.command, None)

    async def _drain_stderr_tail(self, max_bytes: int = 4096) -> str:
        """Return the buffered tail of subprocess stderr (best-effort)."""
        # Give the background reader a moment to flush any final lines.
        await asyncio.sleep(0.1)
        text = "\n".join(self._stderr_tail).strip()
        return text[-max_bytes:]

    async def _start(self) -> None:
        try:
            await self._start_inner()
        except Exception as exc:
            # Nothing else records why a bridge died: setup "succeeds" because
            # handlers are only closures, so without this the provider reports
            # ready and the cause reaches the server's stdout and nowhere else.
            bridge_errors[self.command] = (
                _classify_failure(self._stderr_tail)
                or (self._stderr_tail[-1] if self._stderr_tail else str(exc))
            )
            raise

    async def _start_inner(self) -> None:
        # A new spawn must prove its current credentials before it is reported
        # as authenticated. A later successful handshake adds it back below.
        authenticated_commands.discard(self.command)
        bridge_errors.pop(self.command, None)
        env = self._build_env()
        self._proc = await asyncio.create_subprocess_exec(
            *self._parts,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.cwd,
            env=env,
            limit=STREAM_LIMIT,
        )
        # Begin scraping stderr immediately so an OAuth authorization URL is
        # captured even though the initialize response below blocks until the
        # user finishes authorizing.
        self._start_stderr_reader()
        # initialize handshake
        rid = self._new_id()
        await self._send({
            "jsonrpc": "2.0", "id": rid, "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "mcpproxy", "version": "1.0"},
            },
        })
        # A generous timeout: an OAuth bridge (mcp-remote) holds the handshake
        # open until the interactive browser authorization completes.  With a
        # valid cached token this returns immediately.  self.init_timeout
        # honours the command's own --auth-timeout so we never give up first.
        await self._recv(timeout=self.init_timeout)   # initialize response
        # Handshake completed → any pending authorization is resolved.
        self._clear_pending_auth()
        bridge_errors.pop(self.command, None)
        authenticated_commands.add(self.command)
        # notifications/initialized (no response expected)
        await self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _build_env(self) -> dict[str, str]:
        """Return the env dict for the subprocess.

        Starts from the current process env, then re-reads the proxy's
        ``MCP_ENV_FILE`` (if any) so that secret values added via the UI
        after server start are picked up on the next spawn without
        requiring a full restart.  Only ``env_keys`` are refreshed from
        the file — everything else is inherited unchanged.
        """
        env = os.environ.copy()
        if not self.env_keys:
            return env
        env_file = os.environ.get("MCP_ENV_FILE", ".env")
        try:
            from pathlib import Path
            p = Path(env_file)
            if p.exists():
                for line in p.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, _, v = line.partition("=")
                    k = k.strip()
                    if k in self.env_keys:
                        env[k] = v.strip().strip('"').strip("'")
        except Exception:
            traceback.print_exc()
        return env

    def _alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    # ── public ────────────────────────────────────────────────────────────────

    async def list_tools(self) -> list[dict[str, Any]]:
        async with self._lock:
            if not self._alive():
                await self._start()
            rid = self._new_id()
            await self._send({"jsonrpc": "2.0", "id": rid, "method": "tools/list", "params": {}})
            resp = await self._recv()
        return resp.get("result", {}).get("tools", [])

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        async with self._lock:
            if not self._alive():
                await self._start()
            rid = self._new_id()
            await self._send({
                "jsonrpc": "2.0", "id": rid, "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments},
            })
            resp = await self._recv(timeout=120)

        if "error" in resp:
            err = resp["error"]
            return {"ok": False, "error": err.get("message", str(err))}

        result = resp.get("result", {})
        content: list[dict] = result.get("content", [])
        if not content:
            return {"ok": True, **result}

        parts: list[Any] = []
        for item in content:
            if item.get("type") == "text":
                text = item["text"]
                try:
                    parts.append(json.loads(text))
                except json.JSONDecodeError:
                    parts.append(text)
            else:
                parts.append(item)

        return {"ok": True, "result": parts[0] if len(parts) == 1 else parts}

    async def close(self) -> None:
        self._clear_pending_auth()
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            self._stderr_task = None
        if self._proc:
            try:
                self._proc.stdin.close()  # type: ignore[union-attr]
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except Exception:
                self._proc.kill()
            self._proc = None


# Backward-compatible alias
NpxSession = ProcessSession


# ---------------------------------------------------------------------------
# Module-level session registry  (one session per (command, cwd) pair)
# ---------------------------------------------------------------------------

_sessions: dict[tuple[str, str | None, tuple[str, ...]], ProcessSession] = {}


def get_session(
    command: str,
    cwd: str | None = None,
    env_keys: list[str] | None = None,
) -> ProcessSession:
    """Return (creating if needed) the persistent session for *command*.

    Sessions are keyed on (command, cwd, env_keys) so that two providers
    that share a spawn command but live in different workdirs or use
    different env-key sets get distinct subprocesses.
    """
    key = (command, cwd, tuple(env_keys or ()))
    if key not in _sessions:
        _sessions[key] = ProcessSession(command, cwd=cwd, env_keys=env_keys)
    return _sessions[key]


class ConcurrentSpawnError(RuntimeError):
    """A throwaway spawn was refused because one is already in flight."""


async def introspect(
    command: str,
    cwd: str | None = None,
    env_keys: list[str] | None = None,
) -> list[dict[str, Any]]:
    """
    Spawn a *fresh* process, fetch its tools/list, then shut it down.
    Used by the frontend wizard — does not affect the persistent session registry.

    Refuses to run while another throwaway spawn of the same command is in
    flight.  Two mcp-remote processes for one remote server share the PKCE
    verifier cached under MCP_REMOTE_CONFIG_DIR and overwrite each other, so
    the callback for the first flow then dies with "code_verifier does not
    match the stored code challenge".  Startup warm-up, the wizard and the
    re-authorize button can all reach this at once.

    Persistent tool-call sessions (``get_session``) are deliberately *not*
    guarded: they are already one per command, and blocking a real tool call
    behind a warm-up parked on an OAuth prompt would be worse than the race.
    """
    with _spawn_lock:
        if command in _spawning:
            raise ConcurrentSpawnError(
                f"A bridge for '{command}' is already starting — it may be "
                "waiting for authorization. Wait for it to finish rather than "
                "starting a second one."
            )
        _spawning.add(command)
    session = ProcessSession(command, cwd=cwd, env_keys=env_keys)
    try:
        await session._start()
        return await session.list_tools()
    except Exception as exc:
        traceback.print_exc()
        raise RuntimeError(f"Failed to introspect '{command}': {exc}") from exc
    finally:
        await session.close()
        with _spawn_lock:
            _spawning.discard(command)

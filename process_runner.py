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
import traceback
from typing import Any

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

# Latest pending authorization URL per spawn command, populated from stderr.
# The UI (same process — the frontend runs as a daemon thread inside the MCP
# server) polls this so it can show the link while a spawn is blocked on auth.
pending_auth_urls: dict[str, str] = {}

_URL_RE = re.compile(r"https?://[^\s'\"<>]+")
# Lines that hint mcp-remote (or a similar bridge) is asking the user to
# authorize.  Matched case-insensitively against each stderr line.
_AUTH_HINT_RE = re.compile(
    r"authoriz|oauth|visit (?:this|the following)|open (?:this|the following)",
    re.IGNORECASE,
)


def _extract_auth_url(line: str) -> str | None:
    """Return an authorization URL from *line* if it looks like an auth prompt."""
    if not _AUTH_HINT_RE.search(line):
        return None
    m = _URL_RE.search(line)
    return m.group(0) if m else None


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

    # ── internal ──────────────────────────────────────────────────────────────

    def _new_id(self) -> int:
        self._next_id += 1
        return self._next_id

    async def _send(self, msg: dict[str, Any]) -> None:
        assert self._proc and self._proc.stdin
        data = json.dumps(msg, separators=(",", ":")) + "\n"
        self._proc.stdin.write(data.encode())
        await self._proc.stdin.drain()

    async def _recv(self, timeout: float = 30.0) -> dict[str, Any]:
        assert self._proc and self._proc.stdout
        line = await asyncio.wait_for(self._proc.stdout.readline(), timeout=timeout)
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
                url = _extract_auth_url(line)
                if url:
                    self.pending_auth_url = url
                    pending_auth_urls[self.command] = url
                    print(
                        f"[mcpproxy] authorization required for "
                        f"'{self.command}' — visit: {url}",
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
        env = self._build_env()
        self._proc = await asyncio.create_subprocess_exec(
            *self._parts,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.cwd,
            env=env,
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
        # valid cached token this returns immediately.
        await self._recv(timeout=AUTH_INIT_TIMEOUT)   # initialize response
        # Handshake completed → any pending authorization is resolved.
        self._clear_pending_auth()
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


async def introspect(
    command: str,
    cwd: str | None = None,
    env_keys: list[str] | None = None,
) -> list[dict[str, Any]]:
    """
    Spawn a *fresh* process, fetch its tools/list, then shut it down.
    Used by the frontend wizard — does not affect the persistent session registry.
    """
    session = ProcessSession(command, cwd=cwd, env_keys=env_keys)
    try:
        await session._start()
        return await session.list_tools()
    except Exception as exc:
        traceback.print_exc()
        raise RuntimeError(f"Failed to introspect '{command}': {exc}") from exc
    finally:
        await session.close()

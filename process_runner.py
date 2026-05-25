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
import shlex
import traceback
from typing import Any


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

    async def _drain_stderr_tail(self, max_bytes: int = 4096) -> str:
        """Return up to ``max_bytes`` of buffered stderr from the subprocess."""
        if not self._proc or not self._proc.stderr:
            return ""
        try:
            data = await asyncio.wait_for(
                self._proc.stderr.read(max_bytes), timeout=2.0
            )
        except (asyncio.TimeoutError, Exception):
            return ""
        return data.decode(errors="replace").strip()

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
        await self._recv(timeout=60)   # initialize response
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

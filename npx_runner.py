"""
npx_runner.py — Spawn and talk to npx-based MCP servers over stdio.

Each provider YAML that has an ``npx:`` block (instead of a ``code:`` block)
is handled here.  The npx process speaks the MCP stdio transport:
one JSON-RPC object per line on stdout, stdin for requests.

Two use-cases
─────────────
1. Introspection (frontend wizard): spawn → initialize → tools/list → kill.
2. Tool calls (server): one persistent session per npx command string;
   process is (re-)started on demand and reused across calls.
"""

import asyncio
import json
import shlex
import traceback
from typing import Any


class NpxSession:
    """A long-lived connection to a single npx MCP process."""

    def __init__(self, command: str) -> None:
        self.command = command
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
            raise EOFError("npx process closed stdout")
        return json.loads(line)

    async def _start(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            *self._parts,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
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


# ---------------------------------------------------------------------------
# Module-level session registry  (one session per command string)
# ---------------------------------------------------------------------------

_sessions: dict[str, NpxSession] = {}


def get_session(command: str) -> NpxSession:
    """Return (creating if needed) the persistent session for *command*."""
    if command not in _sessions:
        _sessions[command] = NpxSession(command)
    return _sessions[command]


async def introspect(command: str) -> list[dict[str, Any]]:
    """
    Spawn a *fresh* npx process, fetch its tools/list, then shut it down.
    Used by the frontend wizard — does not affect the persistent session registry.
    """
    session = NpxSession(command)
    try:
        await session._start()
        return await session.list_tools()
    except Exception as exc:
        traceback.print_exc()
        raise RuntimeError(f"Failed to introspect '{command}': {exc}") from exc
    finally:
        await session.close()

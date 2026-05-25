#!/usr/bin/env python3
"""
Agentic MCP + Ollama demo.

Usage:
    python3 tests/ollama_agent.py "Go to https://example.com and summarise"

Environment:
    OLLAMA_BASE   Ollama base URL   (default: http://localhost:11434)
    OLLAMA_MODEL  Ollama model name (default: interactive menu)
    MCP_BASE      MCP server base   (default: http://localhost:8888/mcp)

The script:
  1. Connects to the running MCP server (FastMCP SSE transport).
  2. Lists all registered tools.
  3. Converts MCP inputSchema → Ollama function-calling format.
  4. Shows a numbered model selection menu (or uses $OLLAMA_MODEL if set).
  5. Sends the user prompt to Ollama with all tools available.
  6. Executes tool_calls in a loop until the model produces a final text reply.
"""

import asyncio
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

# ── Configuration ──────────────────────────────────────────────────────────────
OLLAMA_BASE  = os.environ.get("OLLAMA_BASE",  "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "")   # empty = show menu
MCP_BASE     = os.environ.get("MCP_BASE",     "http://localhost:8888/mcp")

MAX_TOOL_ROUNDS = 10  # prevent infinite loops

# ── Low-level HTTP helpers ─────────────────────────────────────────────────────

def _http_post(url: str, payload: dict, timeout: int = 60) -> Any:
    data = json.dumps(payload).encode()
    req  = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _http_get(url: str, timeout: int = 30) -> Any:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


# ── Model selection ───────────────────────────────────────────────────────────

def select_model() -> str:
    """Return the Ollama model to use — env var, or an interactive numbered menu."""
    global OLLAMA_MODEL

    print(f"[agent] Connecting to Ollama at {OLLAMA_BASE} …", end=" ", flush=True)
    try:
        data = _http_get(f"{OLLAMA_BASE}/api/tags", timeout=10)
    except Exception as exc:
        print()
        print(f"[agent] ✗ Cannot reach Ollama: {exc}", file=sys.stderr)
        sys.exit(1)
    print("OK")

    models = [m["name"] for m in data.get("models", [])]
    if not models:
        print("[agent] ✗ No models installed.  Pull one:  ollama pull llama3.2", file=sys.stderr)
        sys.exit(1)

    if OLLAMA_MODEL:
        if OLLAMA_MODEL in models:
            print(f"[agent] Model (env): {OLLAMA_MODEL}")
            return OLLAMA_MODEL
        print(f"[agent] ⚠  OLLAMA_MODEL='{OLLAMA_MODEL}' not found — showing menu.")

    if len(models) == 1:
        print(f"[agent] Auto-selected: {models[0]}")
        return models[0]

    print()
    for i, name in enumerate(models, 1):
        print(f"  {i:>2})  {name}")

    while True:
        try:
            sel = input(f"\nSelect model [1-{len(models)}]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(0)
        if sel.isdigit() and 1 <= int(sel) <= len(models):
            return models[int(sel) - 1]
        print("  Invalid choice.")


# ── MCP helpers ───────────────────────────────────────────────────────────────

def mcp_post(method: str, params: dict | None = None, session_id: str | None = None) -> dict:
    """Send one JSON-RPC request to the MCP server."""
    payload = {
        "jsonrpc": "2.0",
        "id":      1,
        "method":  method,
    }
    if params:
        payload["params"] = params

    url = MCP_BASE
    if session_id:
        url = f"{url}?sessionId={session_id}"

    try:
        return _http_post(url, payload, timeout=60)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise RuntimeError(f"MCP HTTP {e.code} on {method}: {body}") from e


def mcp_initialize() -> str:
    """Run the MCP initialize handshake; return the session ID."""
    resp = mcp_post("initialize", {
        "protocolVersion": "2024-11-05",
        "capabilities":    {},
        "clientInfo":      {"name": "ollama-agent", "version": "0.1"},
    })
    session_id = resp.get("result", {}).get("sessionId", "agent-session")
    mcp_post("notifications/initialized", session_id=session_id)
    return session_id


def mcp_list_tools(session_id: str) -> list[dict]:
    """Return the raw MCP tools list."""
    resp = mcp_post("tools/list", session_id=session_id)
    return resp.get("result", {}).get("tools", [])


def mcp_call_tool(session_id: str, tool_name: str, arguments: dict) -> Any:
    """Call an MCP tool and return its content."""
    resp = mcp_post(
        "tools/call",
        {"name": tool_name, "arguments": arguments},
        session_id=session_id,
    )
    result = resp.get("result", {})
    content = result.get("content", [])
    if not content:
        return result
    if len(content) == 1:
        item = content[0]
        if item.get("type") == "text":
            txt = item.get("text", "")
            try:
                return json.loads(txt)
            except (json.JSONDecodeError, TypeError):
                return txt
    return content


# ── Ollama helpers ────────────────────────────────────────────────────────────

def mcp_tool_to_ollama(tool: dict) -> dict:
    """Convert an MCP tool definition to Ollama function-calling format."""
    schema = tool.get("inputSchema", {})
    return {
        "type": "function",
        "function": {
            "name":        tool["name"],
            "description": tool.get("description", ""),
            "parameters":  schema,
        },
    }


def ollama_chat(messages: list[dict], tools: list[dict], model: str) -> dict:
    """Send a chat completion request to Ollama. Returns the full message dict."""
    payload = {
        "model":    model,
        "messages": messages,
        "tools":    tools,
        "stream":   False,
    }
    resp = _http_post(f"{OLLAMA_BASE}/api/chat", payload, timeout=120)
    return resp.get("message", {})


# ── Agentic loop ──────────────────────────────────────────────────────────────

async def run_agent(user_prompt: str, model: str) -> None:
    print(f"\n[agent] Connecting to MCP at {MCP_BASE} …")
    session_id = mcp_initialize()
    print(f"[agent] Session: {session_id}")

    raw_tools = mcp_list_tools(session_id)
    if not raw_tools:
        print("[agent] No tools registered on the MCP server — exiting.")
        return

    print(f"[agent] {len(raw_tools)} tool(s) available: "
          f"{', '.join(t['name'] for t in raw_tools)}")

    ollama_tools = [mcp_tool_to_ollama(t) for t in raw_tools]

    messages: list[dict] = [{"role": "user", "content": user_prompt}]

    for round_num in range(1, MAX_TOOL_ROUNDS + 1):
        print(f"\n[agent] Round {round_num}: sending to Ollama ({model}) …")
        reply = ollama_chat(messages, ollama_tools, model)

        messages.append(reply)

        tool_calls = reply.get("tool_calls", [])
        if not tool_calls:
            final_text = reply.get("content", "")
            print("\n" + "═" * 60)
            print("FINAL ANSWER")
            print("═" * 60)
            print(final_text)
            return

        for tc in tool_calls:
            fn        = tc.get("function", {})
            tool_name = fn.get("name", "")
            arguments = fn.get("arguments", {})
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    arguments = {}

            print(f"[agent]   → calling tool '{tool_name}' with {json.dumps(arguments)}")
            try:
                result = mcp_call_tool(session_id, tool_name, arguments)
            except Exception as exc:
                result = {"error": str(exc)}
                print(f"[agent]   ✗ tool error: {exc}")
            else:
                preview = json.dumps(result)
                if len(preview) > 200:
                    preview = preview[:197] + "…"
                print(f"[agent]   ✓ result: {preview}")

            messages.append({
                "role":    "tool",
                "content": json.dumps(result),
            })

    print("[agent] Reached max tool rounds without a final answer.")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 tests/ollama_agent.py '<prompt>'")
        sys.exit(1)

    prompt = " ".join(sys.argv[1:])
    model  = select_model()

    print(f"\n[agent] Prompt: {prompt}")
    print(f"[agent] Model:  {model}")

    asyncio.run(run_agent(prompt, model))

"""
Config-driven MCP server.

Each YAML file in MCP_TOOL_CONFIG_DIR defines one *provider*.  Two kinds:

  Code provider    — has a ``code:`` block with async Python functions.
  Package provider — has a ``package:`` block; tool calls are proxied to
                     the subprocess over stdio (no code block needed).
                     Supports any command: npx, uvx, python -m, or an
                     installed binary.
                     Legacy ``npx:`` key is also accepted (backward compat).

Provider YAML keys:
  code:            Python source executed once at startup (code providers).
  package:
    command:       Full command to spawn the MCP server, e.g.:
                     "npx @playwright/mcp@latest --isolated"
                     "uvx mcp-server-fetch"
                     "python -m mcp_server_github"
                     "mcp-server-github"
  requirements:    List of pip packages installed before the server starts.
  setup_commands:  List of shell commands run on every server startup
                   (e.g. "npx playwright install chrome").
  tools:           List of tool declarations:
    name           — unique MCP tool name (advertised as
                     "<provider>__<name>", with the provider's filename
                     normalized to [a-zA-Z0-9-])
    function       — async function name from code block (code providers only)
    description    — shown to the LLM
    enabled        — optional bool, default true; when false the tool is
                     not registered (kept in YAML so you can re-enable
                     it without re-typing the schema)
    input_schema   — JSON Schema object
    secrets.env    — maps handler arg names to environment variable names
    auth           — arbitrary dict forwarded to context["auth"]

No changes to this file are needed when adding new tools or providers.
The HTTP frontend (UI) runs on port 8889 alongside the MCP server on 8888.
"""

import builtins
import inspect
import os
import re
import shlex
import subprocess
import sys
import threading
import traceback
from pathlib import Path
from typing import Any, Callable, Optional

import uvicorn
import yaml
from fastmcp import Context, FastMCP

from config import (
    CONFIG_DIR,
    MCP_HOST,
    MCP_PORT,
    SERVER_NAME,
    UI_HOST,
    UI_PORT,
)

mcp = FastMCP(SERVER_NAME)

SECRET_KEYS = {"password", "token", "secret", "api_key", "apikey", "authorization"}

_JSON_TYPE_MAP: dict[str, type] = {
    "string": str,
    "integer": int,
    "boolean": bool,
    "number": float,
    "object": dict,
    "array": list,
}

SUBPROCESS_KEYS = ("package",)

ADVERTISED_NAME_SEP = "__"


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def normalize_provider_name(name: str) -> str:
    """Normalize a provider name for use as a tool-name prefix.

    Any character outside [a-zA-Z0-9] is replaced with a single ``-`` so the
    result is a stable, MCP-safe identifier (callers can prepend it to a
    tool name with ``__`` to namespace it: ``playwright__browser_navigate``).
    """
    return re.sub(r"[^a-zA-Z0-9]", "-", name or "")


def advertised_tool_name(provider_name: str, tool_name: str) -> str:
    """Return the namespaced tool name advertised to MCP clients."""
    return f"{normalize_provider_name(provider_name)}{ADVERTISED_NAME_SEP}{tool_name}"


def tool_is_enabled(tool_spec: dict[str, Any]) -> bool:
    """Return False only when the spec explicitly sets ``enabled: false``."""
    return tool_spec.get("enabled", True) is not False


def redact_secrets(value: Any) -> Any:
    try:
        if isinstance(value, dict):
            redacted: dict[Any, Any] = {}
            for key, item in value.items():
                key_text = str(key).lower()
                if any(s in key_text for s in SECRET_KEYS):
                    redacted[key] = "[REDACTED]"
                else:
                    redacted[key] = redact_secrets(item)
            return redacted
        if isinstance(value, list):
            return [redact_secrets(item) for item in value]
        return value
    except Exception as exc:
        print(f"redact_secrets error: {exc}")
        traceback.print_exc()
        return "[REDACTION_ERROR]"


def load_provider_specs(config_dir: Path) -> list[dict[str, Any]]:
    """Load all YAML files from config_dir; each is one provider spec."""
    try:
        specs: list[dict[str, Any]] = []
        for path in sorted(config_dir.glob("*.yaml")):
            with path.open("r", encoding="utf-8") as f:
                spec = yaml.safe_load(f) or {}
                spec["_config_path"] = str(path)
                specs.append(spec)
        return specs
    except Exception as exc:
        print(f"load_provider_specs error: {exc}")
        traceback.print_exc()
        raise


def exec_provider_code(spec: dict[str, Any]) -> dict[str, Any]:
    """Execute the provider's ``code`` block; return the resulting namespace."""
    code = spec.get("code", "")
    if not code:
        return {}
    source_path = spec.get("_config_path", "<yaml>")
    namespace: dict[str, Any] = {"__builtins__": builtins}
    try:
        exec(compile(code, source_path, "exec"), namespace)
    except Exception as exc:
        print(f"exec_provider_code error in {source_path}: {exc}")
        traceback.print_exc()
        raise
    return namespace


def resolve_env_defaults(tool_spec: dict[str, Any], kwargs: dict[str, Any]) -> dict[str, Any]:
    """Inject secrets from environment variables into kwargs."""
    try:
        resolved = dict(kwargs)
        env_map = (tool_spec.get("secrets") or {}).get("env", {})
        for arg_name, env_name in env_map.items():
            secret_value = os.environ.get(env_name)
            if not secret_value:
                raise RuntimeError(f"Missing required secret environment variable: {env_name}")
            resolved[arg_name] = secret_value
        return resolved
    except Exception as exc:
        print(f"resolve_env_defaults error: {exc}")
        traceback.print_exc()
        raise


def build_runtime_context(tool_spec: dict[str, Any], ctx: Context | None) -> dict[str, Any]:
    """Assemble the context dict passed as the first argument to every tool function."""
    try:
        return {
            "tool_name": tool_spec["name"],
            "tool_description": tool_spec.get("description", ""),
            "auth": tool_spec.get("auth", {}),
            "mcp_context": ctx,
        }
    except Exception as exc:
        print(f"build_runtime_context error: {exc}")
        traceback.print_exc()
        raise


def _build_typed_signature(
    tool_spec: dict[str, Any],
) -> tuple[inspect.Signature, dict[str, Any]]:
    """Return (Signature, annotations_dict) derived from the tool's input_schema."""
    input_schema = tool_spec.get("input_schema", {})
    properties: dict[str, Any] = input_schema.get("properties", {})
    required_fields: set[str] = set(input_schema.get("required", []))

    params: list[inspect.Parameter] = [
        inspect.Parameter("ctx", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=Context)
    ]
    annotations: dict[str, Any] = {"ctx": Context, "return": Any}

    for param_name, param_spec in properties.items():
        json_type = param_spec.get("type", "string")
        py_type: type = _JSON_TYPE_MAP.get(json_type, str)

        if param_name in required_fields:
            annotation: Any = py_type
            default = inspect.Parameter.empty
        else:
            annotation = Optional[py_type]  # type: ignore[assignment]
            default = param_spec.get("default", None)

        params.append(
            inspect.Parameter(
                param_name,
                inspect.Parameter.KEYWORD_ONLY,
                annotation=annotation,
                default=default,
            )
        )
        annotations[param_name] = annotation

    return inspect.Signature(params, return_annotation=Any), annotations


def register_tool(
    tool_spec: dict[str, Any],
    handler: Callable[..., Any],
    advertised_name: str | None = None,
) -> None:
    """Register a single MCP tool backed by the given async handler function.

    ``advertised_name`` is the name shown to MCP clients (defaults to
    ``tool_spec["name"]``).  Provider-loaded tools pass a namespaced value
    such as ``playwright__browser_navigate`` so tools from different
    providers cannot collide.  ``tool_spec["name"]`` itself is the
    upstream / unprefixed name used when proxying to subprocesses.
    """
    try:
        exposed_name = advertised_name or tool_spec["name"]

        async def dynamic_tool(ctx: Context, **kwargs: Any) -> Any:
            try:
                resolved_kwargs = resolve_env_defaults(tool_spec, kwargs)
                runtime_context = build_runtime_context(tool_spec, ctx)
                return await handler(context=runtime_context, **resolved_kwargs)
            except Exception as exc:
                print(f"dynamic_tool error in {exposed_name}: {exc}")
                traceback.print_exc()
                return {"ok": False, "error": str(exc), "tool": exposed_name}

        dynamic_tool.__name__ = exposed_name
        sig, annotations = _build_typed_signature(tool_spec)
        dynamic_tool.__signature__ = sig          # type: ignore[attr-defined]
        dynamic_tool.__annotations__ = annotations

        mcp.tool(name=exposed_name, description=tool_spec.get("description", ""))(dynamic_tool)
        from tool_registry import register as _tool_registry_register
        _tool_registry_register(exposed_name, tool_spec, dynamic_tool)
        print(f"Registered tool: {exposed_name}")
    except Exception as exc:
        print(f"register_tool error for '{tool_spec.get('name')}': {exc}")
        traceback.print_exc()
        raise


def _get_package_command(spec: dict[str, Any]) -> str | None:
    """Return the spawn command for package providers, or None for code providers."""
    sub = spec.get("package")
    if sub:
        return (sub.get("command") or "").strip() or None
    return None


def _make_process_handler(command: str, tool_name: str) -> Callable[..., Any]:
    """Return an async handler that proxies calls to a subprocess MCP process."""
    from process_runner import get_session

    async def process_handler(context: dict[str, Any], **kwargs: Any) -> Any:
        try:
            session = get_session(command)
            return await session.call_tool(tool_name, kwargs)
        except Exception as exc:
            traceback.print_exc()
            return {"ok": False, "error": str(exc), "tool": tool_name}

    process_handler.__name__ = tool_name
    return process_handler


def register_provider(spec: dict[str, Any]) -> None:
    """Register all tools declared in one provider spec.

    The advertised name of each tool is ``<provider>__<tool>``, where
    ``<provider>`` comes from the YAML filename (normalized via
    ``normalize_provider_name``).  Tools whose spec carries
    ``enabled: false`` are skipped entirely — they remain in the YAML so
    they can be flipped back on without re-typing the schema.
    """
    source_path = spec.get("_config_path", "<unknown>")
    provider_name = Path(source_path).stem if source_path != "<unknown>" else ""
    try:
        command = _get_package_command(spec)

        if command is not None:
            # ── package provider (npx / uvx / python -m / any binary) ──────────
            for tool_spec in spec.get("tools", []):
                tool_name = tool_spec.get("name", "<unnamed>")
                if not tool_is_enabled(tool_spec):
                    print(f"Skipping disabled tool: {advertised_tool_name(provider_name, tool_name)}")
                    continue
                handler = _make_process_handler(command, tool_name)
                register_tool(
                    tool_spec,
                    handler,
                    advertised_name=advertised_tool_name(provider_name, tool_name),
                )

        else:
            # ── code provider ─────────────────────────────────────────────────
            namespace = exec_provider_code(spec)
            tools = spec.get("tools", [])
            if not tools:
                print(f"Warning: no tools declared in {source_path}")
                return

            for tool_spec in tools:
                tool_name = tool_spec.get("name", "<unnamed>")
                if not tool_is_enabled(tool_spec):
                    print(f"Skipping disabled tool: {advertised_tool_name(provider_name, tool_name)}")
                    continue
                function_name = tool_spec.get("function")
                if not function_name:
                    raise ValueError(
                        f"Tool '{tool_name}' in {source_path} is missing required 'function' field"
                    )
                handler = namespace.get(function_name)
                if handler is None:
                    raise RuntimeError(
                        f"Function '{function_name}' (tool '{tool_name}') not found "
                        f"in the code block of {source_path}"
                    )
                register_tool(
                    tool_spec,
                    handler,
                    advertised_name=advertised_tool_name(provider_name, tool_name),
                )

    except Exception as exc:
        print(f"register_provider error in {source_path}: {exc}")
        traceback.print_exc()
        raise


def run_provider_setup(spec: dict[str, Any]) -> None:
    """Install requirements and run setup commands declared in a provider spec.

    Runs synchronously at startup so that every ``docker restart`` re-executes
    the setup steps. pip is a no-op when packages are already installed.
    """
    source_path = spec.get("_config_path", "<unknown>")
    try:
        for req in spec.get("requirements", []):
            if not req:
                continue
            print(f"Installing requirement '{req}' for {source_path}")
            subprocess.run(
                [sys.executable, "-m", "pip", "install", req],
                check=True,
            )
        for cmd in spec.get("setup_commands", []):
            if not cmd:
                continue
            print(f"Running setup command: {cmd}")
            subprocess.run(shlex.split(cmd), check=True)
    except Exception as exc:
        print(f"run_provider_setup error in {source_path}: {exc}")
        traceback.print_exc()
        raise


# ---------------------------------------------------------------------------
# Built-in tools (always available, no YAML config required)
# ---------------------------------------------------------------------------

def register_builtin_tools() -> None:
    """Register the mcpproxy__listfiles and mcpproxy__getfile utility tools.

    These tools expose read-only access to the files directory (default:
    ``.playwright-mcp``, override with ``MCPPROXY_FILES_DIR``).  They are
    always registered regardless of what YAML providers are loaded, giving
    LLMs a way to retrieve screenshots, JSON snapshots, and other files
    produced by package providers such as the Playwright MCP server.
    """
    try:
        from builtin_tools import get_file, list_files

        register_tool(
            {
                "name": "mcpproxy__listfiles",
                "description": (
                    "List files and directories inside the mcpproxy files directory "
                    "(default: .playwright-mcp, override with MCPPROXY_FILES_DIR). "
                    "Use this to discover screenshots, JSON snapshots, and other files "
                    "produced by package providers such as the Playwright MCP server. "
                    "Pass a subdirectory path to drill down."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": (
                                "Subdirectory to list, relative to the base files directory. "
                                "Omit or pass an empty string to list the root."
                            ),
                            "default": "",
                        }
                    },
                    "required": [],
                },
            },
            list_files,
        )

        register_tool(
            {
                "name": "mcpproxy__getfile",
                "description": (
                    "Read the contents of a file from the mcpproxy files directory "
                    "(default: .playwright-mcp). "
                    "Returns UTF-8 text for text files (JSON, HTML, Markdown, …) or "
                    "base64-encoded bytes for binary files (PNG screenshots, …). "
                    "Use mcpproxy__listfiles first to discover available file paths."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Path to the file, relative to the base files directory.",
                        },
                        "encoding": {
                            "type": "string",
                            "description": (
                                "How to encode the returned content. "
                                "'auto' (default) tries UTF-8 and falls back to base64. "
                                "'text' forces UTF-8 (error on binary). "
                                "'base64' always returns base64 (safe for images)."
                            ),
                            "default": "auto",
                        },
                    },
                    "required": ["path"],
                },
            },
            get_file,
        )

        print("Registered built-in tools: mcpproxy__listfiles, mcpproxy__getfile")
    except Exception as exc:
        print(f"register_builtin_tools error: {exc}")
        traceback.print_exc()
        raise


# ---------------------------------------------------------------------------
# Load all providers at import time
# ---------------------------------------------------------------------------

register_builtin_tools()

for provider_spec in load_provider_specs(CONFIG_DIR):
    register_provider(provider_spec)
    run_provider_setup(provider_spec)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _run_ui() -> None:
    try:
        from frontend.app import create_app
        ui_app = create_app()
        uvicorn.run(ui_app, host=UI_HOST, port=UI_PORT, log_level="warning")
    except Exception as exc:
        print(f"UI server error: {exc}")
        traceback.print_exc()


if __name__ == "__main__":
    try:
        ui_thread = threading.Thread(target=_run_ui, daemon=True, name="ui-server")
        ui_thread.start()
        print(f"UI server starting on http://{UI_HOST}:{UI_PORT}")
        mcp.run(transport="streamable-http", host=MCP_HOST, port=MCP_PORT)
    except Exception as exc:
        print(f"main error: {exc}")
        traceback.print_exc()
        raise

"""
Config-driven MCP server.

Each YAML file in MCP_TOOL_CONFIG_DIR defines one *provider*.  A provider has:

  code:   (string) — Python source executed once at startup.  Define all
                     helper functions and async tool functions here.
  repo:   (dict)   — Optional external git repo to clone/pull before executing
                     the code block.  See repo_loader.py for the full schema.
  tools:  (list)   — Each entry declares one MCP tool:
            name        — unique MCP tool name
            function    — name of the async function defined in `code`
            description — shown to the LLM
            input_schema — JSON Schema object (drives argument generation)
            secrets.env — maps handler arg names to environment variable names
            auth        — arbitrary dict forwarded to context["auth"]

No changes to this file are needed when adding new tools or providers.

The HTTP frontend (UI) runs on port 8889 alongside the MCP server on 8888.
"""

import builtins
import inspect
import os
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

# Map JSON Schema types to Python types for signature introspection.
_JSON_TYPE_MAP: dict[str, type] = {
    "string": str,
    "integer": int,
    "boolean": bool,
    "number": float,
    "object": dict,
    "array": list,
}


# ---------------------------------------------------------------------------
# Pure helpers (all tested in tests/test_server.py)
# ---------------------------------------------------------------------------

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
    """Execute the provider's top-level `code` block; return the namespace.

    The code string is compiled with the YAML file path as the source name so
    that tracebacks point to the right location.
    """
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
    """Inject secrets from environment variables into kwargs before calling the handler."""
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


def register_tool(tool_spec: dict[str, Any], handler: Callable[..., Any]) -> None:
    """Register a single MCP tool backed by the given async handler function."""
    try:
        async def dynamic_tool(ctx: Context, **kwargs: Any) -> Any:
            try:
                resolved_kwargs = resolve_env_defaults(tool_spec, kwargs)
                runtime_context = build_runtime_context(tool_spec, ctx)
                return await handler(context=runtime_context, **resolved_kwargs)
            except Exception as exc:
                print(f"dynamic_tool error in {tool_spec.get('name')}: {exc}")
                traceback.print_exc()
                return {
                    "ok": False,
                    "error": str(exc),
                    "tool": tool_spec.get("name"),
                }

        dynamic_tool.__name__ = tool_spec["name"]

        sig, annotations = _build_typed_signature(tool_spec)
        dynamic_tool.__signature__ = sig  # type: ignore[attr-defined]
        dynamic_tool.__annotations__ = annotations

        mcp.tool(
            name=tool_spec["name"],
            description=tool_spec.get("description", ""),
        )(dynamic_tool)

        print(f"Registered tool: {tool_spec['name']}")
    except Exception as exc:
        print(f"register_tool error for '{tool_spec.get('name')}': {exc}")
        traceback.print_exc()
        raise


def register_provider(spec: dict[str, Any]) -> None:
    """Execute the provider's code block and register all its declared tools.

    If the spec contains a ``repo:`` block, the repo is cloned/pulled first
    and its path is added to sys.path before the code block executes.
    """
    source_path = spec.get("_config_path", "<unknown>")
    try:
        # ── Optional external repo ─────────────────────────────────────────
        if spec.get("repo"):
            from repo_loader import setup_repo
            provider_name = Path(source_path).stem
            setup_repo(spec, provider_name)

        namespace = exec_provider_code(spec)

        tools = spec.get("tools", [])
        if not tools:
            print(f"Warning: no tools declared in {source_path}")
            return

        for tool_spec in tools:
            tool_name = tool_spec.get("name", "<unnamed>")
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

            register_tool(tool_spec, handler)

    except Exception as exc:
        print(f"register_provider error in {source_path}: {exc}")
        traceback.print_exc()
        raise


# ---------------------------------------------------------------------------
# Load all providers at import time
# ---------------------------------------------------------------------------

for provider_spec in load_provider_specs(CONFIG_DIR):
    register_provider(provider_spec)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _run_ui() -> None:
    """Start the HTTP frontend in a background daemon thread."""
    try:
        from frontend.app import create_app
        ui_app = create_app()
        uvicorn.run(ui_app, host=UI_HOST, port=UI_PORT, log_level="warning")
    except Exception as exc:
        print(f"UI server error: {exc}")
        traceback.print_exc()


if __name__ == "__main__":
    try:
        # Start UI server (non-blocking daemon thread)
        ui_thread = threading.Thread(target=_run_ui, daemon=True, name="ui-server")
        ui_thread.start()
        print(f"UI server starting on http://{UI_HOST}:{UI_PORT}")

        # Run MCP server (blocking)
        mcp.run(transport="streamable-http", host=MCP_HOST, port=MCP_PORT)
    except Exception as exc:
        print(f"main error: {exc}")
        traceback.print_exc()
        raise

"""
Shared tool registry — populated by server.py at startup, read by frontend/app.py.

server.py calls ``register()`` inside ``register_tool()`` for every enabled tool
(including the two built-ins).  frontend/app.py reads ``get_all()`` / ``get()``
to serve the OpenAI-compatible ``/v1/tools`` endpoints.

Both modules run in the same Python process (the UI runs in a daemon thread), so
a plain module-level dict is sufficient — no IPC or shared-memory machinery needed.
"""

from typing import Any, Callable

# name → {"spec": tool_spec_dict, "handler": dynamic_tool_callable}
_registry: dict[str, dict[str, Any]] = {}


def register(name: str, spec: dict[str, Any], handler: Callable[..., Any]) -> None:
    """Add or replace a tool in the registry.

    ``name``    — the advertised MCP tool name (e.g. ``playwright__browser_navigate``)
    ``spec``    — the tool_spec dict (contains ``description``, ``input_schema``, …)
    ``handler`` — the ``dynamic_tool`` async callable built in ``register_tool()``
    """
    _registry[name] = {"spec": spec, "handler": handler}


def get(name: str) -> dict[str, Any] | None:
    """Return the registry entry for ``name``, or ``None`` if not found."""
    return _registry.get(name)


def get_all() -> dict[str, dict[str, Any]]:
    """Return a shallow copy of the full registry (name → entry)."""
    return dict(_registry)


def clear() -> None:
    """Remove all entries.  Used only in tests."""
    _registry.clear()

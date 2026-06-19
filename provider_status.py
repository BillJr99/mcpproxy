"""
Shared provider-readiness registry — populated by server.py, read by both
server.py's gated tool handlers and frontend/app.py (for a "still initializing"
banner in the UI).

Background:
    server.py registers every provider's tools immediately as *gated stubs*
    (built from the YAML tool list only — no pip/exec/network), then runs the
    slow per-provider setup (pip install / build / setup_commands) on a
    background thread.  Until a provider's setup finishes, calls to its tools
    return a "still initializing, retry shortly" directive instead of failing.

    This module holds the per-provider readiness state that the gated handlers
    consult, plus the real handlers that replace the stubs once setup completes.

Like tool_registry.py, every reader runs in the same Python process (the UI runs
in a daemon thread), so a plain module-level dict is sufficient — no IPC needed.
This module has NO import-time side effects so it is safe to import from anywhere.
"""

from dataclasses import dataclass, field
from typing import Any, Callable

# A provider that has been registered but whose dependencies are still
# installing.  Once setup finishes it flips to "ready"; if building its real
# handlers fails it flips to "failed".
PENDING = "pending"
READY = "ready"
FAILED = "failed"


@dataclass
class ProviderState:
    """Readiness of one provider and (once ready) its real tool handlers.

    ``name``     — the provider name (YAML filename stem), used in messages.
    ``status``   — one of ``PENDING`` / ``READY`` / ``FAILED``.
    ``error``    — failure detail when ``status == FAILED``, else ``None``.
    ``handlers`` — advertised tool name → real async handler, populated when
                   the provider becomes ready.  Gated stubs delegate here.
    """

    name: str
    status: str = PENDING
    error: str | None = None
    handlers: dict[str, Callable[..., Any]] = field(default_factory=dict)


# provider name → ProviderState
_states: dict[str, ProviderState] = {}


def set_state(state: ProviderState) -> None:
    """Register (or replace) a provider's readiness state."""
    _states[state.name] = state


def get(name: str) -> ProviderState | None:
    """Return the state for ``name``, or ``None`` if unknown."""
    return _states.get(name)


def all_states() -> dict[str, ProviderState]:
    """Return a shallow copy of the full provider → state map."""
    return dict(_states)


def clear() -> None:
    """Remove all states.  Used only in tests."""
    _states.clear()

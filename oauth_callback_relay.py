"""
oauth_callback_relay.py — complete an OAuth flow from a callback URL the user pastes.

Why this exists
───────────────
Remote MCP providers bridged by ``mcp-remote`` redirect the approving browser to
``http://localhost:<port>/oauth/callback?code=…``.  That only resolves when the
browser runs on the same machine as mcpproxy.  Authorize from a laptop while
mcpproxy runs on a server and the code lands in the laptop's address bar with
nothing listening behind it.

``mcp-remote`` owns its PKCE verifier, so mcpproxy cannot exchange a pasted code
itself.  What it *can* do is replay the callback against the loopback listener
``mcp-remote`` is still holding inside this container — which is what this module
is for.  (mcpproxy's own flows — REST ``authorization_code`` and provider
``oauth:`` blocks — keep their state in ``rest_provider.AuthCodeTokenStore`` and
are completed in-process instead; see ``frontend.app``.)

Security contract
─────────────────
* The replay host is the hardcoded literal ``127.0.0.1``.  The scheme, host and
  port of the pasted URL are read and discarded; the port comes only from
  server-side state keyed by a command that is *currently* pending.
* Only a fixed whitelist of query parameters is forwarded, so nothing extra can
  be smuggled through into ``mcp-remote``.
* The pasted value carries a live authorization code.  It is never logged, never
  echoed in a response, and never embedded in an exception message — including
  the exception messages this module raises, which is why every ``httpx`` error
  is re-raised with ``from None`` (httpx puts the full request URL in its own
  ``__str__`` and in ``__context__``).
"""

from __future__ import annotations

import re
import shlex
import socket
from urllib.parse import parse_qsl, urlsplit

import httpx

# Default path mcp-remote (and mcpproxy's own UI route) serves the callback on.
DEFAULT_CALLBACK_PATH = "/oauth/callback"

# How long to wait for the loopback listener to accept the replayed callback.
DELIVERY_TIMEOUT_SECONDS = 10.0

# A pasted callback URL is a URL, not a document.
MAX_INPUT_CHARS = 4096

# `npx -y mcp-remote@0.1.38`, a bare `mcp-remote`, or an absolute path to it.
_MCP_REMOTE_TOKEN_RE = re.compile(r"^(?:.*/)?mcp-remote(?:@\S*)?$")

# Query parameters forwarded to the listener.  Everything else is dropped:
# this is what stops a crafted paste from reaching mcp-remote with extras.
_ALLOWED_PARAMS = (
    "code",
    "state",
    "error",
    "error_description",
    "error_uri",
    "iss",
    "scope",
    "session_state",
)

_SAFE_PATH_RE = re.compile(r"^/[A-Za-z0-9._~/-]{0,128}$")
# A URL, as opposed to a bare query string.  Anchored deliberately: a query
# value can legitimately contain "://" (``iss=https://issuer``,
# ``error_uri=https://…``), so a substring test would misread the whole paste.
_URL_START_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
# A sanity check, not a format assertion: providers issue codes in wildly
# different shapes (base64url, JWTs, opaque handles with punctuation), so this
# only rejects whitespace, control characters and absurd lengths.  httpx
# percent-encodes on the way out, so nothing here is an injection guard.
_OPAQUE_RE = re.compile(r"^[^\s\x00-\x1f\x7f]{1,2048}$")


class CallbackInputError(ValueError):
    """The pasted value could not be used.

    The message is safe to show to the user and never contains the input.
    """


class CallbackDeliveryError(RuntimeError):
    """Replaying the callback failed.

    The message never contains the authorization code.
    """


# ---------------------------------------------------------------------------
# Where to deliver: resolving the loopback callback port
# ---------------------------------------------------------------------------

def callback_port_from_command(command: str) -> int | None:
    """Return the callback port declared in an ``mcp-remote`` spawn command.

    ``mcp-remote``'s usage is ``mcp-remote <server-url> [callback-port] [flags…]``,
    so positionals are read only up to the first ``-``-prefixed token.  That one
    rule is what keeps a flag value such as ``--auth-timeout 600`` from being
    mistaken for the port.
    """
    try:
        parts = shlex.split(command)
    except ValueError:  # unbalanced quotes
        return None
    index = next(
        (i for i, part in enumerate(parts) if _MCP_REMOTE_TOKEN_RE.match(part)), None
    )
    if index is None:
        return None
    positionals: list[str] = []
    for token in parts[index + 1:]:
        if token.startswith("-"):
            break
        positionals.append(token)
    if len(positionals) < 2:
        return None
    try:
        port = int(positionals[1])
    except ValueError:
        return None
    return port if 1 <= port <= 65535 else None


def resolve_callback_port(command: str) -> tuple[int | None, str | None]:
    """Return ``(port, source)`` for *command*; source is 'stderr' | 'command' | None.

    The scraped value wins: when the YAML omits the port argument, mcp-remote
    picks one at random and announcing it on stderr is the only way to know it.
    """
    # Imported here rather than at module scope so process_runner can keep
    # importing cheaply and neither module depends on the other's import order.
    from process_runner import callback_listener_ports

    port = callback_listener_ports.get(command)
    if port:
        return port, "stderr"
    port = callback_port_from_command(command)
    return (port, "command") if port else (None, None)


# ---------------------------------------------------------------------------
# What to deliver: parsing the pasted value
# ---------------------------------------------------------------------------

def parse_callback_input(raw: str) -> tuple[str, dict[str, str]]:
    """Parse a pasted callback URL (or bare query string) into (path, params).

    Accepts the full URL from the browser's address bar, a bare ``?code=…&state=…``,
    or just ``code=…&state=…``.
    """
    if not isinstance(raw, str):
        raise CallbackInputError("Paste the callback URL as text.")
    # Browsers wrap long URLs when copied out of an error page.
    value = "".join(raw.split())
    if not value:
        raise CallbackInputError("Paste the callback URL from the browser address bar.")
    if len(value) > MAX_INPUT_CHARS:
        raise CallbackInputError("That is too long to be an OAuth callback URL.")
    value = value.split("#", 1)[0]

    if _URL_START_RE.match(value):
        split = urlsplit(value)
        # The scheme, host and port are deliberately read and discarded: the
        # replay target is decided server-side, never by the pasted value.
        path = split.path or DEFAULT_CALLBACK_PATH
        query = split.query
    else:
        path = DEFAULT_CALLBACK_PATH
        query = value[1:] if value.startswith("?") else value

    if not _SAFE_PATH_RE.match(path) or ".." in path.split("/"):
        raise CallbackInputError("Unexpected callback path in the pasted URL.")

    pairs = parse_qsl(query, keep_blank_values=False)
    params = {k: v for k, v in pairs if k in _ALLOWED_PARAMS}

    if "code" not in params:
        error = params.get("error")
        if error:
            # The provider's error slug is safe to echo; error_description is
            # attacker-influencable free text, so it stays out of the message.
            raise CallbackInputError(
                f"The provider returned an OAuth error: {error}. "
                "Start the authorization again."
            )
        raise CallbackInputError(
            "No code= parameter found. Paste the full URL from the browser "
            "address bar after approving access."
        )

    for key in ("code", "state"):
        if key in params and not _OPAQUE_RE.match(params[key]):
            raise CallbackInputError(
                f"The {key} value has an unexpected format — paste the URL unmodified."
            )
    return path, params


# ---------------------------------------------------------------------------
# Delivery + liveness
# ---------------------------------------------------------------------------

async def deliver_to_bridge(port: int, path: str, params: dict[str, str]) -> int:
    """Replay a callback against the loopback listener mcp-remote is holding.

    The host is a hardcoded literal; *port* must come from ``resolve_callback_port``.
    Returns the listener's status code.
    """
    try:
        # trust_env=False is load-bearing: an HTTPS_PROXY / ALL_PROXY in the
        # container environment must never see a URL carrying a live code.
        async with httpx.AsyncClient(
            timeout=DELIVERY_TIMEOUT_SECONDS, trust_env=False, follow_redirects=False
        ) as client:
            response = await client.get(
                f"http://127.0.0.1:{port}{path}",
                params=params,
                # What a browser reaching the published Docker port would send.
                headers={"Host": f"localhost:{port}"},
            )
    except httpx.ConnectError:
        # `from None` throughout: httpx embeds the request URL — and therefore
        # the authorization code — in both its message and its __context__.
        raise CallbackDeliveryError(
            f"Nothing is listening on 127.0.0.1:{port}. The bridge is no longer "
            "waiting for a callback — start the authorization again, then paste "
            "the URL promptly."
        ) from None
    except httpx.HTTPError:
        raise CallbackDeliveryError(
            f"Could not deliver the callback to 127.0.0.1:{port}."
        ) from None
    if response.status_code >= 400:
        raise CallbackDeliveryError(
            f"The bridge rejected the callback (HTTP {response.status_code})."
        )
    return response.status_code


def probe_loopback_port(port: int, timeout: float = 0.35) -> bool:
    """Return whether anything accepts TCP on 127.0.0.1:*port* right now.

    Probing container loopback is the only meaningful liveness check: the
    published Docker port is fronted by ``callback_forwarder``, whose bind
    accepts unconditionally and only then discovers there is nothing behind it.
    """
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False

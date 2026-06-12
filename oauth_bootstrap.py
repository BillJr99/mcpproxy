"""
Provider-declared OAuth bootstrap — mint user-consent token files from the UI.

A provider YAML may carry a top-level ``oauth:`` block describing a one-time
browser consent flow whose result is a token *file* the provider's own code
reads (as opposed to the REST providers' header-injection auth, which caches
tokens under ``.rest-auth``)::

    oauth:
      type: google
      client_secret_file: /app/tools/secrets/client_secret.json
      token_file: /app/tools/secrets/gmail_token.json
      scopes:
        - https://www.googleapis.com/auth/gmail.settings.basic
      # optional: prompt (default "consent"), login_hint

Currently only ``type: google`` is supported.  The flow reuses the existing
authorization_code machinery in ``rest_provider``: in-flight attempts register
in ``AuthCodeTokenStore._pending_flows`` (tagged ``kind: "google"`` so the
shared ``GET /oauth/callback`` route can dispatch back here), and the
authorization URL publishes into ``pending_rest_auth`` so the UI banner shows
a clickable link with no frontend polling changes.

The written token file matches ``google.oauth2.credentials.Credentials.to_json()``,
so provider code can load it with ``Credentials.from_authorized_user_file()``
and the google client libraries handle refresh at call time.  The authorize
URL always requests ``access_type=offline`` and (by default) ``prompt=consent``
— Google only issues a refresh_token on consent, not on silent re-approval.

Redirect URI: the approving browser is sent to ``{OAUTH_REDIRECT_BASE}/oauth/callback``
(see ``rest_provider.oauth_redirect_uri`` / MCPPROXY_OAUTH_REDIRECT_BASE).
Google Desktop ("installed") clients accept any http://localhost:<port>
loopback without registration; "web" clients must have the exact URI
registered in the Google Cloud Console.
"""

import datetime
import hashlib
import json
import secrets as _secrets
import time
import traceback
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from rest_provider import (
    HTTP_TIMEOUT,
    AuthCodeTokenStore,
    _b64url,
    oauth_redirect_uri,
    pending_rest_auth,
)

GOOGLE_AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"

SUPPORTED_TYPES = {"google"}


def get_oauth_config(spec: dict[str, Any]) -> dict[str, Any] | None:
    """Return the provider's top-level ``oauth:`` block (with a type), or None."""
    cfg = spec.get("oauth") or {}
    return cfg if cfg.get("type") else None


def load_client_secret(path: str | Path) -> dict[str, str]:
    """Parse a Google ``client_secret.json`` (``installed`` or ``web`` client).

    Returns ``{client_id, client_secret, auth_uri, token_uri}``; raises
    ``ValueError`` with an actionable message on a missing/unreadable file.
    """
    p = Path(path)
    if not p.is_file():
        raise ValueError(
            f"client_secret_file not found: {p} — download it from the Google "
            "Cloud Console and upload it via the Files manager (e.g. tools/secrets/)"
        )
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"client_secret_file {p} is not valid JSON: {exc}")
    key = "installed" if "installed" in raw else "web" if "web" in raw else None
    if key is None:
        raise ValueError(
            f"client_secret_file {p} has neither an 'installed' nor a 'web' "
            "section — is it really a Google OAuth client secret file?"
        )
    section = raw[key] or {}
    client_id = (section.get("client_id") or "").strip()
    if not client_id:
        raise ValueError(f"client_secret_file {p} is missing client_id")
    return {
        "client_id": client_id,
        "client_secret": (section.get("client_secret") or "").strip(),
        "auth_uri": (section.get("auth_uri") or GOOGLE_AUTH_URI).strip(),
        "token_uri": (section.get("token_uri") or GOOGLE_TOKEN_URI).strip(),
    }


def token_status(oauth_cfg: dict[str, Any]) -> dict[str, Any]:
    """Inspect the configured token_file.  Never raises."""
    token_file = (oauth_cfg.get("token_file") or "").strip()
    status = {
        "token_file": token_file,
        "present": False,
        "has_refresh_token": False,
        "expiry": None,
    }
    try:
        data = json.loads(Path(token_file).read_text(encoding="utf-8"))
        status["present"] = True
        status["has_refresh_token"] = bool(data.get("refresh_token"))
        status["expiry"] = data.get("expiry")
    except Exception:
        pass
    return status


# ---------------------------------------------------------------------------
# Begin / complete (dispatch tables keyed by oauth type / flow kind)
# ---------------------------------------------------------------------------

def begin_authorization(provider: str, oauth_cfg: dict[str, Any]) -> str:
    """Build the consent URL for the provider's oauth block and publish it."""
    otype = (oauth_cfg.get("type") or "").strip()
    begin = _BEGINNERS.get(otype)
    if begin is None:
        raise ValueError(
            f"Unsupported oauth.type {otype!r} (supported: {sorted(SUPPORTED_TYPES)})"
        )
    return begin(provider, oauth_cfg)


async def complete_authorization(flow: dict[str, Any], code: str) -> str:
    """Finish a non-REST flow popped from ``AuthCodeTokenStore._pending_flows``."""
    kind = flow.get("kind", "")
    complete = _COMPLETERS.get(kind)
    if complete is None:
        raise RuntimeError(f"Unknown authorization flow kind {kind!r}")
    return await complete(flow, code)


def _begin_google(provider: str, oauth_cfg: dict[str, Any]) -> str:
    client = load_client_secret(oauth_cfg.get("client_secret_file") or "")
    scopes = [s for s in (oauth_cfg.get("scopes") or []) if s]
    code_verifier = _b64url(_secrets.token_bytes(48))
    code_challenge = _b64url(hashlib.sha256(code_verifier.encode("ascii")).digest())
    state = _b64url(_secrets.token_bytes(24))
    redirect_uri = oauth_redirect_uri()
    params = {
        "response_type": "code",
        "client_id": client["client_id"],
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "scope": " ".join(scopes),
        # offline + consent are what make Google return a refresh_token.
        "access_type": "offline",
        "prompt": (oauth_cfg.get("prompt") or "consent").strip(),
    }
    login_hint = (oauth_cfg.get("login_hint") or "").strip()
    if login_hint:
        params["login_hint"] = login_hint
    auth_url = f"{client['auth_uri']}?{urlencode(params)}"

    AuthCodeTokenStore._prune_flows()
    AuthCodeTokenStore._pending_flows[state] = {
        "kind": "google",
        "provider": provider,
        "client_id": client["client_id"],
        "client_secret": client["client_secret"],
        "token_uri": client["token_uri"],
        "token_file": (oauth_cfg.get("token_file") or "").strip(),
        "scopes": scopes,
        "code_verifier": code_verifier,
        "redirect_uri": redirect_uri,
        "created": time.time(),
    }
    pending_rest_auth[provider] = auth_url
    print(
        f"[mcpproxy] authorization required for provider '{provider}' — visit: {auth_url}",
        flush=True,
    )
    return auth_url


async def _complete_google(flow: dict[str, Any], code: str) -> str:
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": flow["redirect_uri"],
        "client_id": flow["client_id"],
        "code_verifier": flow["code_verifier"],
    }
    if flow.get("client_secret"):
        data["client_secret"] = flow["client_secret"]
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        resp = await client.post(flow["token_uri"], data=data)
        resp.raise_for_status()
        payload = resp.json()

    access = payload.get("access_token")
    if not access:
        raise RuntimeError(f"Token endpoint {flow['token_uri']} returned no access_token")

    token_file = Path(flow["token_file"])
    refresh = payload.get("refresh_token")
    if not refresh:
        # Google omits the refresh_token on silent re-approval; an earlier
        # token file may still hold a valid one we can carry forward.
        try:
            refresh = json.loads(token_file.read_text(encoding="utf-8")).get("refresh_token")
        except Exception:
            refresh = None
    if not refresh:
        raise RuntimeError(
            "Google returned no refresh_token. This usually means consent was "
            "granted previously without prompt=consent — revoke the app's access "
            "at https://myaccount.google.com/permissions and authorize again."
        )

    # Granted scopes (space-separated in the response) may differ from requested.
    scope_str = (payload.get("scope") or "").strip()
    scopes = scope_str.split() if scope_str else list(flow.get("scopes") or [])
    expires_in = float(payload.get("expires_in", 3600))
    expiry = datetime.datetime.fromtimestamp(
        time.time() + expires_in, tz=datetime.timezone.utc
    ).replace(tzinfo=None)  # naive UTC — the format Credentials.to_json() uses

    # Exactly the shape google.oauth2.credentials.Credentials.to_json() emits,
    # so from_authorized_user_file() loads it and refreshes transparently.
    record = {
        "token": access,
        "refresh_token": refresh,
        "token_uri": flow["token_uri"],
        "client_id": flow["client_id"],
        "client_secret": flow.get("client_secret") or "",
        "scopes": scopes,
        "universe_domain": "googleapis.com",
        "account": "",
        "expiry": expiry.isoformat(timespec="microseconds") + "Z",
    }
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(json.dumps(record, indent=2), encoding="utf-8")
    print(f"[mcpproxy] OAuth token written for provider '{flow['provider']}': {token_file}")

    pending_rest_auth.pop(flow["provider"], None)
    return access


_BEGINNERS = {"google": _begin_google}
_COMPLETERS = {"google": _complete_google}


# ---------------------------------------------------------------------------
# Startup warm-up
# ---------------------------------------------------------------------------

async def warm_provider(provider: str, oauth_cfg: dict[str, Any]) -> None:
    """Surface the consent URL at startup when no usable token exists.

    Best-effort: a refresh_token on disk counts as ready (the google libs
    refresh at call time); anything else publishes the banner URL.  Never raises.
    """
    try:
        status = token_status(oauth_cfg)
        if status["has_refresh_token"]:
            print(f"[mcpproxy] OAuth token ready for provider: {provider}")
            return
        begin_authorization(provider, oauth_cfg)
    except Exception as exc:  # noqa: BLE001 — warm-up must not break startup
        print(f"[mcpproxy] OAuth warm-up for provider '{provider}' did not complete: {exc}")
        traceback.print_exc()

"""
rest_provider.py — Wrap an arbitrary REST API as MCP tools.

A provider YAML with a ``rest:`` block declares a base URL, an ``auth:`` block,
and either an ``openapi:`` source (expanded into endpoints at create time by the
frontend) or an explicit list of ``endpoints:``.  Each entry in the provider's
``tools:`` list maps 1:1 to an endpoint by ``name``.

This module supplies:

  * ``_make_rest_handler``  — an async handler (the analogue of
    ``server._make_process_handler``) that builds and issues the HTTP request and
    returns parsed JSON, suitable for ``server.register_tool``.
  * ``OAuthTokenManager``   — client_credentials token cache (fetch/cache/refresh).
  * ``AuthCodeTokenStore``  — authorization_code + PKCE token store (on-disk cache,
    interactive browser flow, refresh-token rotation).
  * ``resolve_rest_auth``   — turn an ``auth:`` block into a resolver that mutates
    outgoing request headers.
  * ``introspect_openapi``  — parse an OpenAPI 3.0 document into endpoints + tools.

Secrets (tokens, client id/secret) are referenced by environment-variable name in
the YAML (``*_env`` keys) and read from ``os.environ`` here, so they ride the
existing ``.env`` / Secrets-UI mechanism without ever being written to YAML.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets as _secrets
import time
import traceback
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode

import httpx

from config import OAUTH_REDIRECT_BASE, REST_AUTH_DIR

# Authorization URLs a REST provider is currently waiting on, keyed by provider
# name.  The UI polls this (alongside ``process_runner.pending_auth_urls``) so an
# interactive authorization_code flow surfaces a clickable "Authorize" link.
pending_rest_auth: dict[str, str] = {}

# Seconds of slack subtracted from a token's lifetime so we refresh slightly
# before the real expiry rather than racing it.
_EXPIRY_SKEW = 30.0

# How long an in-flight authorization_code attempt (state + PKCE verifier) stays
# valid before it is pruned.  The user has this long to complete the browser flow.
_FLOW_TTL = float(os.environ.get("MCPPROXY_OAUTH_FLOW_TTL", "600"))

# Default timeout (seconds) for every outbound HTTP request.
HTTP_TIMEOUT = float(os.environ.get("MCPPROXY_REST_TIMEOUT", "30"))


class NeedsAuthorization(Exception):
    """Raised when an authorization_code provider has no usable token.

    Carries the authorization URL the user must visit (also published into
    ``pending_rest_auth``) so the caller can surface it.
    """

    def __init__(self, provider: str, auth_url: str) -> None:
        self.provider = provider
        self.auth_url = auth_url
        super().__init__(
            f"Authorization required for REST provider '{provider}'. "
            f"Visit: {auth_url}"
        )


# ---------------------------------------------------------------------------
# Secret resolution
# ---------------------------------------------------------------------------

def _require_env(env_name: str) -> str:
    value = os.environ.get(env_name)
    if not value:
        raise RuntimeError(f"Missing required secret environment variable: {env_name}")
    return value


# ---------------------------------------------------------------------------
# OAuth2 client_credentials
# ---------------------------------------------------------------------------

class OAuthTokenManager:
    """Fetch/cache/refresh an OAuth2 ``client_credentials`` access token."""

    def __init__(
        self,
        token_url: str,
        client_id_env: str,
        client_secret_env: str,
        scopes: list[str] | None = None,
        extra: dict[str, str] | None = None,
    ) -> None:
        self.token_url = token_url
        self.client_id_env = client_id_env
        self.client_secret_env = client_secret_env
        self.scopes = list(scopes or [])
        self.extra = dict(extra or {})
        self._access_token: str | None = None
        self._expires_at: float = 0.0
        self._lock = asyncio.Lock()

    def _is_expired(self) -> bool:
        return (not self._access_token) or (time.time() >= self._expires_at - _EXPIRY_SKEW)

    async def get_token(self, *, force_refresh: bool = False) -> str:
        async with self._lock:
            if force_refresh or self._is_expired():
                await self._fetch()
            assert self._access_token is not None
            return self._access_token

    async def _fetch(self) -> None:
        data = {
            "grant_type": "client_credentials",
            "client_id": _require_env(self.client_id_env),
            "client_secret": _require_env(self.client_secret_env),
        }
        if self.scopes:
            data["scope"] = " ".join(self.scopes)
        data.update(self.extra)
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            resp = await client.post(self.token_url, data=data)
            resp.raise_for_status()
            payload = resp.json()
        token = payload.get("access_token")
        if not token:
            raise RuntimeError(
                f"Token endpoint {self.token_url} returned no access_token"
            )
        self._access_token = token
        expires_in = float(payload.get("expires_in", 3600))
        self._expires_at = time.time() + expires_in


# One manager per (token_url, client_id_env, scopes) so all endpoints of a
# provider share a single cached token (parallels process_runner._sessions).
_token_managers: dict[tuple, OAuthTokenManager] = {}


def get_token_manager(auth: dict[str, Any]) -> OAuthTokenManager:
    key = (
        auth.get("token_url", ""),
        auth.get("client_id_env", ""),
        tuple(auth.get("scopes") or ()),
    )
    mgr = _token_managers.get(key)
    if mgr is None:
        mgr = OAuthTokenManager(
            token_url=auth.get("token_url", ""),
            client_id_env=auth.get("client_id_env", ""),
            client_secret_env=auth.get("client_secret_env", ""),
            scopes=list(auth.get("scopes") or []),
            extra=dict(auth.get("extra") or {}),
        )
        _token_managers[key] = mgr
    return mgr


# ---------------------------------------------------------------------------
# OAuth2 authorization_code + PKCE
# ---------------------------------------------------------------------------

def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def oauth_redirect_uri() -> str:
    """The redirect URI the OAuth provider must call back; user registers it."""
    return f"{OAUTH_REDIRECT_BASE}/oauth/callback"


class AuthCodeTokenStore:
    """On-disk cache + interactive flow for an authorization_code provider.

    One instance per provider name.  Tokens persist under
    ``REST_AUTH_DIR/<provider>.json`` so they survive restarts.
    """

    # In-flight authorization attempts keyed by the OAuth ``state`` value, shared
    # across all instances (the callback route only has the state to go on).
    _pending_flows: dict[str, dict[str, Any]] = {}

    def __init__(self, provider: str, auth: dict[str, Any]) -> None:
        self.provider = provider
        self.auth = auth
        self._lock = asyncio.Lock()

    # ── persistence ─────────────────────────────────────────────────────────

    def _cache_path(self) -> Path:
        return REST_AUTH_DIR / f"{self.provider}.json"

    def _load(self) -> dict[str, Any]:
        path = self._cache_path()
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            traceback.print_exc()
            return {}

    def _save(self, data: dict[str, Any]) -> None:
        path = self._cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")

    # ── token access ────────────────────────────────────────────────────────

    async def get_token(self, *, force_refresh: bool = False) -> str:
        async with self._lock:
            data = self._load()
            access = data.get("access_token")
            expires_at = float(data.get("expires_at", 0))
            fresh = access and time.time() < expires_at - _EXPIRY_SKEW
            if fresh and not force_refresh:
                return access
            refresh_token = data.get("refresh_token")
            if refresh_token:
                try:
                    return await self._refresh(refresh_token)
                except Exception:
                    traceback.print_exc()
            # No token, or refresh failed → user must (re)authorize.
            auth_url = self.begin_authorization()
            raise NeedsAuthorization(self.provider, auth_url)

    async def _refresh(self, refresh_token: str) -> str:
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": _require_env(self.auth["client_id_env"]),
        }
        secret_env = self.auth.get("client_secret_env")
        if secret_env:
            data["client_secret"] = _require_env(secret_env)
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            resp = await client.post(self.auth["token_url"], data=data)
            resp.raise_for_status()
            payload = resp.json()
        return self._persist_token_response(payload, prior_refresh=refresh_token)

    def _persist_token_response(
        self, payload: dict[str, Any], prior_refresh: str | None = None
    ) -> str:
        access = payload.get("access_token")
        if not access:
            raise RuntimeError("Token endpoint returned no access_token")
        expires_in = float(payload.get("expires_in", 3600))
        record = {
            "access_token": access,
            "refresh_token": payload.get("refresh_token") or prior_refresh,
            "expires_at": time.time() + expires_in,
        }
        self._save(record)
        return access

    # ── interactive authorization ─────────────────────────────────────────────

    def begin_authorization(self) -> str:
        """Build the authorize URL (with PKCE), register the in-flight flow, and
        publish the URL into ``pending_rest_auth``.  Returns the URL.
        """
        code_verifier = _b64url(_secrets.token_bytes(48))
        code_challenge = _b64url(hashlib.sha256(code_verifier.encode("ascii")).digest())
        state = _b64url(_secrets.token_bytes(24))
        redirect_uri = oauth_redirect_uri()
        params = {
            "response_type": "code",
            "client_id": _require_env(self.auth["client_id_env"]),
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        scopes = self.auth.get("scopes") or []
        if scopes:
            params["scope"] = " ".join(scopes)
        auth_url = f"{self.auth['authorize_url']}?{urlencode(params)}"
        self._prune_flows()
        AuthCodeTokenStore._pending_flows[state] = {
            "provider": self.provider,
            "auth": self.auth,
            "code_verifier": code_verifier,
            "redirect_uri": redirect_uri,
            "created": time.time(),
        }
        pending_rest_auth[self.provider] = auth_url
        print(
            f"[mcpproxy] authorization required for REST provider "
            f"'{self.provider}' — visit: {auth_url}",
            flush=True,
        )
        return auth_url

    @classmethod
    def _prune_flows(cls) -> None:
        """Drop in-flight authorization attempts older than ``_FLOW_TTL``."""
        cutoff = time.time() - _FLOW_TTL
        stale = [s for s, f in cls._pending_flows.items() if f.get("created", 0) < cutoff]
        for state in stale:
            cls._pending_flows.pop(state, None)

    @classmethod
    async def complete_authorization(cls, state: str, code: str) -> str:
        """Exchange ``code`` for tokens using the flow registered under ``state``."""
        cls._prune_flows()
        flow = cls._pending_flows.pop(state, None)
        if flow is None:
            raise RuntimeError("Unknown or expired authorization state")
        auth = flow["auth"]
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": flow["redirect_uri"],
            "client_id": _require_env(auth["client_id_env"]),
            "code_verifier": flow["code_verifier"],
        }
        secret_env = auth.get("client_secret_env")
        if secret_env:
            data["client_secret"] = _require_env(secret_env)
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            resp = await client.post(auth["token_url"], data=data)
            resp.raise_for_status()
            payload = resp.json()
        store = cls(flow["provider"], auth)
        access = store._persist_token_response(payload)
        pending_rest_auth.pop(flow["provider"], None)
        return access


# ---------------------------------------------------------------------------
# Auth resolver
# ---------------------------------------------------------------------------

class _AuthResolver:
    """Applies a provider's auth to outgoing request headers."""

    def __init__(self, provider_name: str, auth: dict[str, Any]) -> None:
        self.provider_name = provider_name
        self.auth = auth or {}
        self.type = (self.auth.get("type") or "none").strip()
        self.supports_retry = self.type in ("client_credentials", "authorization_code")
        self._auth_code_store: AuthCodeTokenStore | None = None
        if self.type == "authorization_code":
            self._auth_code_store = AuthCodeTokenStore(provider_name, self.auth)

    async def apply(self, headers: dict[str, str], *, force_refresh: bool = False) -> None:
        if self.type == "none":
            return
        if self.type == "bearer":
            headers["Authorization"] = f"Bearer {_require_env(self.auth['token_env'])}"
        elif self.type == "api_key":
            header_name = self.auth.get("header", "X-Api-Key")
            prefix = self.auth.get("prefix", "")
            value = _require_env(self.auth["value_env"])
            headers[header_name] = f"{prefix}{value}" if prefix else value
        elif self.type == "client_credentials":
            token = await get_token_manager(self.auth).get_token(force_refresh=force_refresh)
            headers["Authorization"] = f"Bearer {token}"
        elif self.type == "authorization_code":
            assert self._auth_code_store is not None
            token = await self._auth_code_store.get_token(force_refresh=force_refresh)
            headers["Authorization"] = f"Bearer {token}"
        else:
            raise RuntimeError(f"Unsupported auth type: {self.type!r}")


def resolve_rest_auth(provider_name: str, rest_config: dict[str, Any]) -> _AuthResolver:
    return _AuthResolver(provider_name, rest_config.get("auth") or {})


# ---------------------------------------------------------------------------
# Request handler
# ---------------------------------------------------------------------------

def _split_kwargs(
    endpoint_spec: dict[str, Any], kwargs: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Partition kwargs into (path_params, query_params, body) per the endpoint."""
    path_names = set(endpoint_spec.get("path_params") or [])
    query_names = set(endpoint_spec.get("query_params") or [])
    body_names = set(endpoint_spec.get("body_params") or [])
    path: dict[str, Any] = {}
    query: dict[str, Any] = {}
    body: dict[str, Any] = {}
    for key, value in kwargs.items():
        if value is None:
            continue
        if key in path_names:
            path[key] = value
        elif key in query_names:
            query[key] = value
        elif key in body_names:
            body[key] = value
        else:
            # Unclassified args: assume query for GET/DELETE, body otherwise.
            method = (endpoint_spec.get("method") or "GET").upper()
            if method in ("GET", "DELETE", "HEAD"):
                query[key] = value
            else:
                body[key] = value
    return path, query, body


def _make_rest_handler(
    endpoint_spec: dict[str, Any],
    rest_config: dict[str, Any],
    provider_name: str,
) -> Callable[..., Any]:
    """Return an async handler that calls one REST endpoint.

    Signature matches what ``server.register_tool`` expects:
    ``async handler(context=..., **kwargs)``.
    """
    base_url = (rest_config.get("base_url") or "").rstrip("/")
    default_headers = dict(rest_config.get("headers") or {})
    method = (endpoint_spec.get("method") or "GET").upper()
    path_template = endpoint_spec.get("path") or "/"
    tool_name = endpoint_spec.get("name", "<rest>")
    resolver = resolve_rest_auth(provider_name, rest_config)

    async def rest_handler(context: dict[str, Any], **kwargs: Any) -> Any:
        try:
            path_params, query, body = _split_kwargs(endpoint_spec, kwargs)
            path = path_template.format(**path_params)
            url = f"{base_url}{path}"

            async def _do(force_refresh: bool) -> httpx.Response:
                headers = dict(default_headers)
                await resolver.apply(headers, force_refresh=force_refresh)
                async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                    return await client.request(
                        method,
                        url,
                        params=query or None,
                        json=body or None,
                        headers=headers,
                    )

            resp = await _do(force_refresh=False)
            if resp.status_code == 401 and resolver.supports_retry:
                resp = await _do(force_refresh=True)

            if resp.status_code >= 400:
                return {
                    "ok": False,
                    "error": f"HTTP {resp.status_code}: {resp.text[:500]}",
                    "status": resp.status_code,
                    "tool": tool_name,
                }

            try:
                return resp.json()
            except (json.JSONDecodeError, ValueError):
                return {"ok": True, "status": resp.status_code, "text": resp.text}
        except NeedsAuthorization as exc:
            return {"ok": False, "error": str(exc), "auth_url": exc.auth_url, "tool": tool_name}
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            return {"ok": False, "error": str(exc), "tool": tool_name}

    rest_handler.__name__ = tool_name
    return rest_handler


# ---------------------------------------------------------------------------
# OpenAPI introspection
# ---------------------------------------------------------------------------

_HTTP_METHODS = ("get", "put", "post", "delete", "patch", "head", "options")

_JSON_DEFAULT_TYPE = "string"


def _resolve_ref(doc: dict[str, Any], node: Any) -> Any:
    """Resolve a single local ``$ref`` (one level) within ``doc``."""
    if isinstance(node, dict) and "$ref" in node:
        ref = node["$ref"]
        if ref.startswith("#/"):
            target: Any = doc
            for part in ref[2:].split("/"):
                if not isinstance(target, dict):
                    return {}
                target = target.get(part, {})
            return target
    return node


def _param_schema_type(schema: dict[str, Any]) -> str:
    t = schema.get("type")
    if isinstance(t, str):
        return t
    return _JSON_DEFAULT_TYPE


def introspect_openapi(
    source: str, base_url: str | None = None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Parse an OpenAPI 3.0 document into (endpoints, tools).

    ``source`` is a URL (fetched via httpx) or a local file path.  Returns a list
    of endpoint specs (method/path/param classification) and a parallel list of
    tool specs (name/description/input_schema) ready to drop into the provider.
    """
    raw = _load_openapi_source(source)
    doc = _parse_openapi_text(raw)
    endpoints: list[dict[str, Any]] = []
    tools: list[dict[str, Any]] = []
    used_names: set[str] = set()

    paths = doc.get("paths") or {}
    for path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        shared_params = path_item.get("parameters") or []
        for method in _HTTP_METHODS:
            op = path_item.get(method)
            if not isinstance(op, dict):
                continue
            name = _operation_name(op, method, path, used_names)
            used_names.add(name)
            params = list(shared_params) + list(op.get("parameters") or [])
            endpoint, tool = _build_endpoint_and_tool(doc, name, method, path, op, params)
            endpoints.append(endpoint)
            tools.append(tool)

    return endpoints, tools


def _load_openapi_source(source: str) -> str:
    if source.startswith("http://") or source.startswith("https://"):
        resp = httpx.get(source, timeout=HTTP_TIMEOUT, follow_redirects=True)
        resp.raise_for_status()
        return resp.text
    return Path(source).read_text(encoding="utf-8")


def _parse_openapi_text(text: str) -> dict[str, Any]:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        import yaml  # local import; pyyaml is always installed

        return yaml.safe_load(text) or {}


def _operation_name(op: dict[str, Any], method: str, path: str, used: set[str]) -> str:
    name = (op.get("operationId") or "").strip()
    if not name:
        # Derive from method + path: POST /users/{id}/items → post_users_id_items
        slug = path.strip("/").replace("/", "_").replace("{", "").replace("}", "")
        slug = "".join(c if (c.isalnum() or c == "_") else "_" for c in slug)
        name = f"{method}_{slug}".strip("_") or method
    # Sanitize to a tool-safe identifier.
    name = "".join(c if (c.isalnum() or c in "_-") else "_" for c in name)
    candidate = name
    n = 2
    while candidate in used:
        candidate = f"{name}_{n}"
        n += 1
    return candidate


def _build_endpoint_and_tool(
    doc: dict[str, Any],
    name: str,
    method: str,
    path: str,
    op: dict[str, Any],
    params: list[Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    path_params: list[str] = []
    query_params: list[str] = []
    body_params: list[str] = []
    properties: dict[str, Any] = {}
    required: list[str] = []

    for raw_param in params:
        param = _resolve_ref(doc, raw_param)
        if not isinstance(param, dict):
            continue
        pname = param.get("name")
        if not pname:
            continue
        location = param.get("in")
        schema = _resolve_ref(doc, param.get("schema") or {})
        properties[pname] = {
            "type": _param_schema_type(schema),
            "description": param.get("description", ""),
        }
        if param.get("required") or location == "path":
            required.append(pname)
        if location == "path":
            path_params.append(pname)
        elif location == "query":
            query_params.append(pname)

    # requestBody → body params (application/json schema).
    request_body = _resolve_ref(doc, op.get("requestBody") or {})
    if isinstance(request_body, dict):
        content = request_body.get("content") or {}
        json_media = content.get("application/json") or {}
        body_schema = _resolve_ref(doc, json_media.get("schema") or {})
        body_props = body_schema.get("properties") or {}
        body_required = set(body_schema.get("required") or [])
        for bname, bschema in body_props.items():
            bschema = _resolve_ref(doc, bschema)
            properties[bname] = {
                "type": _param_schema_type(bschema),
                "description": bschema.get("description", ""),
            }
            body_params.append(bname)
            if bname in body_required or request_body.get("required"):
                if bname in body_required:
                    required.append(bname)

    endpoint = {
        "name": name,
        "method": method.upper(),
        "path": path,
        "path_params": path_params,
        "query_params": query_params,
        "body_params": body_params,
    }
    description = (op.get("summary") or op.get("description") or name).strip()
    tool = {
        "name": name,
        "description": description or name,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": required,
        },
    }
    return endpoint, tool

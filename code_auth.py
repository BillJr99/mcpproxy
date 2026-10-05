"""
code_auth.py — Managed sign-in for native ``code:`` providers.

A code provider may declare a top-level ``auth:`` block using the same schema as
a REST provider's ``rest.auth`` (``bearer``, ``api_key``, ``client_credentials``,
``authorization_code``) or the built-in ``device_code`` type::

    auth:
      type: authorization_code
      inject_as: access_token          # required: names the handler argument
      authorize_url: https://example.com/oauth/authorize
      token_url: https://example.com/oauth/token
      client_id_env: EXAMPLE_CLIENT_ID
      client_secret_env: EXAMPLE_CLIENT_SECRET
      redirect_uri: https://example.com/  # optional per-provider redirect

mcpproxy resolves the credential before calling the handler and passes it as a
hidden keyword argument (``inject_as``); for ``device_code`` it may map several
resources to several arguments.  The block is inert unless it carries both
``type`` and ``inject_as``, so a provider that never declared one, or uses the
key for something else, behaves exactly as before.

When the user must sign in, the tool call returns (it does not raise)::

    {"ok": false, "status": "authorization_required", "authorize_url": ...,
     "message": ..., "tool": ...}

and the link is published to the UI's pending-authorization banner.

Refresh contract: when a handler's result is a dict with ``status`` (or
``status_code``) 401 and ``ok`` not true, or it raises an exception carrying
401, mcpproxy refreshes the token once and calls the handler once more, for
refreshable types (client_credentials, authorization_code, device_code).
``auth.retry_on_401: false`` (provider) or ``retry_on_401: false`` (one tool)
turns that off, for write tools that may fail with 401 after a partial write.
Handlers can also drive auth themselves through ``context["mcpproxy_auth"]``.

Never logs or returns a token, code, client secret, or caller key.
"""

from __future__ import annotations

import asyncio
import time
import traceback
from typing import Any, Callable

SUPPORTED_TYPES = ("bearer", "api_key", "client_credentials", "authorization_code", "device_code")
REFRESHABLE_TYPES = ("client_credentials", "authorization_code", "device_code")

# How long a model should wait before calling again after a device-code prompt.
RETRY_AFTER_SECONDS = 15


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def get_code_auth(spec: dict[str, Any]) -> dict[str, Any] | None:
    """Return a code provider's managed ``auth:`` block, or None when not opted in.

    Only code providers qualify (not ``rest:``/``package:``/``repository:``),
    and only a block with both ``type`` and ``inject_as``.
    """
    if not isinstance(spec, dict):
        return None
    if any(spec.get(k) is not None for k in ("rest", "package", "npx", "repository")):
        return None
    auth = spec.get("auth")
    if not isinstance(auth, dict):
        return None
    if not str(auth.get("type") or "").strip() or not auth.get("inject_as"):
        return None
    return auth


def inject_map(auth: dict[str, Any]) -> dict[str | None, str]:
    """``{resource-or-None: handler argument name}`` from ``inject_as``."""
    inject = auth.get("inject_as")
    if isinstance(inject, dict):
        return {str(res): str(arg) for res, arg in inject.items() if arg}
    if isinstance(inject, str) and inject.strip():
        return {None: inject.strip()}
    return {}


def _tool_arg_names(tool: dict[str, Any]) -> tuple[set[str], set[str]]:
    """(input_schema property names, hidden secrets argument names) for one tool."""
    props = ((tool.get("input_schema") or {}).get("properties") or {})
    params = {str(p) for p in props} if isinstance(props, dict) else set()
    secrets_cfg = tool.get("secrets") or {}
    hidden: set[str] = set()
    for block in ("env", "headers"):
        mapping = secrets_cfg.get(block) or {}
        if isinstance(mapping, dict):
            hidden.update(str(a) for a in mapping)
    return params, hidden


def validate_auth_config(auth: dict[str, Any], tools: list[dict[str, Any]]) -> list[str]:
    """Validation errors for a code provider's managed ``auth:`` block.

    ``tools`` are raw YAML tool entries (``input_schema`` / ``secrets``).  Shared
    by the UI validator and by ``wrap_handler`` so a hand-edited file that never
    went through the UI is held to the same rules.
    """
    errors: list[str] = []
    atype = str(auth.get("type") or "").strip()
    if atype not in SUPPORTED_TYPES:
        return [f"auth.type must be one of {list(SUPPORTED_TYPES)}"]

    inject = auth.get("inject_as")
    mapping = inject_map(auth)
    if isinstance(inject, dict) and atype != "device_code":
        errors.append("auth.inject_as may be a mapping only for device_code auth")
    if not mapping:
        errors.append("auth.inject_as must name the handler argument that receives the credential")
    arg_names = list(mapping.values())
    if len(set(arg_names)) != len(arg_names):
        errors.append("auth.inject_as names must be distinct")
    for arg in arg_names:
        if not arg.isidentifier() or arg == "context":
            errors.append(f"auth.inject_as '{arg}' must be a Python identifier other than 'context'")

    # A2: an injected credential must never overwrite (or be overwritten by) a
    # tool parameter or another hidden secret argument.
    for tool in tools or []:
        params, hidden = _tool_arg_names(tool)
        tname = tool.get("name") or "?"
        for arg in arg_names:
            if arg in params:
                errors.append(f"auth.inject_as '{arg}' clashes with a parameter of tool '{tname}'")
            if arg in hidden:
                errors.append(f"auth.inject_as '{arg}' clashes with a secrets argument of tool '{tname}'")

    for key in ("retry_on_401", "warm_on_start"):
        if key in auth and not isinstance(auth[key], bool):
            errors.append(f"auth.{key} must be true or false")

    if atype == "bearer":
        if bool(str(auth.get("token_env") or "").strip()) == bool(str(auth.get("token_file") or "").strip()):
            errors.append("bearer auth requires exactly one of auth.token_env or auth.token_file")
    elif atype == "api_key":
        if not str(auth.get("value_env") or "").strip():
            errors.append("auth.value_env is required for api_key auth")
    elif atype == "client_credentials":
        for key in ("token_url", "client_id_env", "client_secret_env"):
            if not str(auth.get(key) or "").strip():
                errors.append(f"auth.{key} is required for client_credentials auth")
    elif atype == "authorization_code":
        for key in ("authorize_url", "token_url", "client_id_env"):
            if not str(auth.get(key) or "").strip():
                errors.append(f"auth.{key} is required for authorization_code auth")
        errors.extend(validate_redirect(auth))
    elif atype == "device_code":
        errors.extend(_validate_device_code(auth, tools, mapping))
    return errors


def validate_redirect(auth: dict[str, Any]) -> list[str]:
    """Checks for the optional per-provider ``redirect_uri`` / ``redirect_uri_env``."""
    errors: list[str] = []
    uri = str(auth.get("redirect_uri") or "").strip()
    if uri and not (uri.startswith("https://") or uri.startswith("http://")):
        errors.append("auth.redirect_uri must be a full http(s) URL")
    if uri and str(auth.get("redirect_uri_env") or "").strip():
        errors.append("auth.redirect_uri and auth.redirect_uri_env are mutually exclusive")
    return errors


def _validate_device_code(
    auth: dict[str, Any], tools: list[dict[str, Any]], mapping: dict[str | None, str]
) -> list[str]:
    errors: list[str] = []
    for key in ("device_authorization_url", "token_url"):
        if not str(auth.get(key) or "").strip():
            errors.append(f"auth.{key} is required for device_code auth")
    if not (
        str(auth.get("client_id") or "").strip()
        or str(auth.get("client_id_env") or "").strip()
        or auth.get("fallback_client_ids")
    ):
        errors.append("device_code auth needs auth.client_id, auth.client_id_env or auth.fallback_client_ids")
    resources = auth.get("resources")
    if not isinstance(resources, dict) or not resources:
        errors.append("auth.resources must map resource names to their scopes")
        return errors
    default = str(auth.get("default_resource") or next(iter(resources)))
    if default not in resources:
        errors.append(f"auth.default_resource '{default}' is not one of auth.resources")
    for res in mapping:
        if res is not None and res not in resources:
            errors.append(f"auth.inject_as names unknown resource '{res}'")
    for tool in tools or []:
        wanted = tool.get("auth_resources")
        if wanted is None:
            continue
        if not isinstance(wanted, list) or any(str(r) not in resources for r in wanted):
            errors.append(f"tools '{tool.get('name') or '?'}': auth_resources must list names from auth.resources")
    secret_arg = str(auth.get("encrypt_with_secret") or "").strip()
    if secret_arg:
        for tool in tools or []:
            _params, hidden = _tool_arg_names(tool)
            if secret_arg not in hidden:
                errors.append(
                    f"auth.encrypt_with_secret '{secret_arg}' must be declared under "
                    f"secrets.env or secrets.headers of tool '{tool.get('name') or '?'}'"
                )
    return errors


def auth_env_keys(auth: dict[str, Any]) -> list[str]:
    """Environment-variable names an auth block reads (never their values)."""
    keys = ("token_env", "value_env", "client_id_env", "client_secret_env", "redirect_uri_env", "tenant_env")
    return [str(auth[k]) for k in keys if auth.get(k)]


# ---------------------------------------------------------------------------
# Provider-level auth
# ---------------------------------------------------------------------------

class AuthorizationRequired(Exception):
    """The user must sign in; ``result`` is the structured tool result."""

    def __init__(self, result: dict[str, Any]) -> None:
        super().__init__(result.get("message") or "authorization required")
        self.result = result


class ProviderAuth:
    """Credential resolution for one code provider's ``auth:`` block."""

    def __init__(self, provider: str, auth: dict[str, Any]) -> None:
        self.provider = provider
        self.auth = auth
        self.type = str(auth.get("type") or "").strip()
        self.refreshable = self.type in REFRESHABLE_TYPES
        self._resolver = None
        self._device = None
        if self.type == "device_code":
            from device_code_auth import DeviceCodeStore

            self._device = DeviceCodeStore(provider, auth)
        else:
            from rest_provider import resolve_rest_auth

            self._resolver = resolve_rest_auth(provider, {"auth": auth})

    @property
    def device(self):
        return self._device

    async def token(
        self,
        resource: str | None = None,
        *,
        key: str | None = None,
        force_refresh: bool = False,
        begin_if_needed: bool = True,
    ) -> str:
        """Resolve one credential, or raise ``AuthorizationRequired``."""
        if self._device is not None:
            return await self._device_token(resource, key, force_refresh, begin_if_needed)
        from rest_provider import NeedsAuthorization, auth_redirect_uri, oauth_redirect_uri

        try:
            return await self._resolver.resolve_credential(
                force_refresh=force_refresh, begin_if_needed=begin_if_needed
            )
        except NeedsAuthorization as exc:
            redirect = auth_redirect_uri(self.auth)
            manual = redirect != oauth_redirect_uri()
            message = (
                f"Sign-in required for '{self.provider}'. Open authorize_url and approve "
                "(the link is also in the mcpproxy UI's pending-authorization banner), "
                "then call this tool again."
            )
            if manual:
                message += (
                    " After approving, the browser lands on the provider's redirect page; "
                    "copy that full address (it carries code= and state=) into the mcpproxy "
                    "UI's manual OAuth callback dialog promptly, before the code expires."
                )
            raise AuthorizationRequired({
                "ok": False,
                "status": "authorization_required",
                "authorize_url": exc.auth_url,
                "redirect_uri": redirect,
                "manual_callback_required": manual,
                "message": message,
            }) from None

    async def _device_token(
        self, resource: str | None, key: str | None, force_refresh: bool, begin_if_needed: bool
    ) -> str:
        from device_code_auth import DeviceAuthNeeded

        store = self._device
        try:
            return await asyncio.to_thread(store.access_token, resource, key, force_refresh)
        except DeviceAuthNeeded as exc:
            if not begin_if_needed:
                raise AuthorizationRequired({
                    "ok": False,
                    "status": "authorization_required",
                    "resource": exc.resource,
                    "message": f"Sign-in required for '{self.provider}'.",
                }) from None
            flow = await asyncio.to_thread(store.start, exc.resource, key, reason=exc.reason)
            raise AuthorizationRequired(device_prompt(self.provider, flow, exc.reason)) from None

    async def status(self, key: str | None = None) -> dict[str, Any]:
        if self._device is not None:
            return await asyncio.to_thread(self._device.status, key)
        from rest_provider import AuthCodeTokenStore, auth_redirect_uri, pending_rest_auth

        out: dict[str, Any] = {"provider": self.provider, "type": self.type}
        if self.type == "authorization_code":
            data = AuthCodeTokenStore(self.provider, self.auth)._load()
            out["signed_in"] = bool(data.get("refresh_token") or data.get("access_token"))
            out["expires_in"] = max(0, int(float(data.get("expires_at") or 0) - time.time()))
            out["redirect_uri"] = auth_redirect_uri(self.auth)
            if self.provider in pending_rest_auth:
                out["authorize_url"] = pending_rest_auth[self.provider]
        return out

    async def login(self, resource: str | None = None, key: str | None = None) -> dict[str, Any]:
        if self._device is not None:
            flow = await asyncio.to_thread(self._device.start, resource, key)
            return device_prompt(self.provider, flow, "")
        if self.type != "authorization_code":
            return {"ok": True, "message": f"'{self.provider}' uses {self.type} auth; nothing to sign in to."}
        from rest_provider import AuthCodeTokenStore

        url = AuthCodeTokenStore(self.provider, self.auth).begin_authorization()
        return {"ok": True, "status": "authorization_started", "authorize_url": url}

    async def logout(self) -> dict[str, Any]:
        if self._device is not None:
            return await asyncio.to_thread(self._device.logout)
        if self.type == "authorization_code":
            from rest_provider import AuthCodeTokenStore, pending_rest_auth

            path = AuthCodeTokenStore(self.provider, self.auth)._cache_path()
            removed = path.exists()
            path.unlink(missing_ok=True)
            pending_rest_auth.pop(self.provider, None)
            return {"ok": True, "removed_cache": removed}
        return {"ok": True, "removed_cache": False}


def device_prompt(provider: str, flow: dict[str, Any], reason: str) -> dict[str, Any]:
    """The ``authorization_required`` result for a device-code sign-in."""
    why = {
        "consent_required": f"The '{flow['resource']}' resource needs its own one-time approval. ",
        "expired": "The previous sign-in expired or was revoked. ",
    }.get(reason, "")
    link = flow.get("verification_uri_complete") or flow.get("verification_uri")
    return {
        "ok": False,
        "status": "authorization_required",
        "authorize_url": link,
        "verification_uri": flow.get("verification_uri"),
        "user_code": flow.get("user_code"),
        "expires_in": flow.get("expires_in"),
        "resource": flow.get("resource"),
        "retry_after_seconds": RETRY_AFTER_SECONDS,
        "message": why + (
            f"To sign in to '{provider}', open {flow.get('verification_uri')} and enter the "
            f"code {flow.get('user_code')}. mcpproxy finishes the sign-in in the background; "
            f"call this tool again in about {RETRY_AFTER_SECONDS} seconds."
        ),
    }


# Shared per-provider instances, so every tool of a provider uses one store.
_providers: dict[str, tuple[int, ProviderAuth]] = {}


def provider_auth(provider: str, auth: dict[str, Any]) -> ProviderAuth:
    marker = hash(repr(sorted(auth.items(), key=lambda kv: kv[0])))
    cached = _providers.get(provider)
    if cached is None or cached[0] != marker:
        cached = (marker, ProviderAuth(provider, auth))
        _providers[provider] = cached
    return cached[1]


# ---------------------------------------------------------------------------
# Handler-facing hook
# ---------------------------------------------------------------------------

class AuthHandle:
    """``context["mcpproxy_auth"]``: lets a handler drive its own auth.

    Bound to one call: it holds that call's caller key (for an encrypted
    device-code cache) only for the call's lifetime and never exposes it.
    """

    __slots__ = ("_pa", "_key")

    def __init__(self, pa: ProviderAuth, key: str | None) -> None:
        self._pa = pa
        self._key = key

    def __repr__(self) -> str:
        return f"<AuthHandle provider={self._pa.provider!r} type={self._pa.type!r}>"

    @property
    def provider(self) -> str:
        return self._pa.provider

    @property
    def type(self) -> str:
        return self._pa.type

    async def get_token(self, resource: str | None = None, force_refresh: bool = False) -> str:
        """Return a token, forcing a refresh when asked.  Raises ``AuthorizationRequired``."""
        return await self._pa.token(resource, key=self._key, force_refresh=force_refresh)

    async def status(self) -> dict[str, Any]:
        return await self._pa.status(self._key)

    async def login(self, resource: str | None = None) -> dict[str, Any]:
        return await self._pa.login(resource, self._key)

    async def logout(self) -> dict[str, Any]:
        return await self._pa.logout()


# ---------------------------------------------------------------------------
# Wrapper
# ---------------------------------------------------------------------------

def _http_status(outcome: Any) -> int | None:
    """An explicit integer HTTP status on a result dict or an exception (bools excluded)."""
    candidates: list[Any] = []
    if isinstance(outcome, BaseException):
        for attr in ("status_code", "status", "http_status"):
            candidates.append(getattr(outcome, attr, None))
        response = getattr(outcome, "response", None)
        if response is not None:
            candidates.append(getattr(response, "status_code", None))
    elif isinstance(outcome, dict):
        if outcome.get("ok") is True:
            return None
        candidates.extend([outcome.get("status"), outcome.get("status_code")])
    for value in candidates:
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


class ManagedAuthError(RuntimeError):
    """A handler exception whose text carried an injected credential, scrubbed.

    Keeps the original HTTP status (``status_code``) so mcpproxy's caller-header
    fallback still sees an explicit 401/403.
    """

    def __init__(self, message: str, status_code: int | None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _scrub(text: str, values: list[str]) -> str:
    for value in sorted(set(values), key=len, reverse=True):
        if value and len(value) >= 4:
            text = text.replace(value, "[REDACTED]")
    return text


def _sanitize(exc: BaseException, values: list[str]) -> BaseException:
    text = f"{exc}"
    if not any(v and len(v) >= 4 and v in text for v in values):
        return exc
    return ManagedAuthError(
        f"{type(exc).__name__}: {_scrub(text, values)}", _http_status(exc)
    )


def wrap_handler(
    provider: str,
    auth: dict[str, Any],
    handler: Callable[..., Any],
    tool_spec: dict[str, Any],
    all_tools: list[dict[str, Any]] | None = None,
) -> Callable[..., Any]:
    """Return ``handler`` wrapped with managed auth for one tool.

    Raises ``ValueError`` (failing this provider's setup, visibly) when the auth
    block is invalid, including an ``inject_as`` name clash.
    """
    errors = validate_auth_config(auth, all_tools if all_tools is not None else [tool_spec])
    if errors:
        raise ValueError(f"Provider '{provider}' auth block is invalid: " + "; ".join(errors))

    pa = provider_auth(provider, auth)
    mapping = inject_map(auth)
    wanted = tool_spec.get("auth_resources")
    if pa.type == "device_code":
        default = pa.device.default_resource
        mapping = {(res or default): arg for res, arg in mapping.items()}
        if isinstance(wanted, list):
            allowed = {str(r) for r in wanted}
            mapping = {res: arg for res, arg in mapping.items() if res in allowed}
    retry_on_401 = bool(tool_spec.get("retry_on_401", auth.get("retry_on_401", True)))
    secret_arg = str(auth.get("encrypt_with_secret") or "").strip()
    tool_name = str(tool_spec.get("name") or getattr(handler, "__name__", "tool"))

    async def _inject(key: str | None, force: bool, only: str | None) -> dict[str, str]:
        out: dict[str, str] = {}
        for res, arg in mapping.items():
            refresh = force and (only is None or res == only)
            out[arg] = await pa.token(res, key=key, force_refresh=refresh)
        return out

    async def _call(context: Any, kwargs: dict[str, Any], injected: dict[str, str], handle: AuthHandle) -> Any:
        ctx = dict(context) if isinstance(context, dict) else {}
        ctx["mcpproxy_auth"] = handle
        try:
            return await handler(context=ctx, **kwargs, **injected)
        except Exception as exc:
            raise _sanitize(exc, list(injected.values())) from None

    async def managed_auth_handler(context: Any = None, **kwargs: Any) -> Any:
        key = kwargs.get(secret_arg) if secret_arg else None
        handle = AuthHandle(pa, key)
        try:
            injected = await _inject(key, False, None)
        except AuthorizationRequired as exc:
            return {**exc.result, "tool": tool_name}
        except Exception as exc:
            return _auth_error(exc, tool_name)

        try:
            result = await _call(context, kwargs, injected, handle)
        except AuthorizationRequired as exc:
            # Raised by the handler's own context["mcpproxy_auth"].get_token().
            return {**exc.result, "tool": tool_name}
        except Exception as exc:
            if not (retry_on_401 and pa.refreshable and _http_status(exc) == 401):
                raise
            narrow = None
        else:
            if not (retry_on_401 and pa.refreshable and _http_status(result) == 401):
                return result
            narrow = result.get("auth_resource") if isinstance(result, dict) else None

        # One forced refresh, then exactly one more call.
        print(f"[code_auth] {provider}/{tool_name}: HTTP 401; refreshing the token and retrying once", flush=True)
        try:
            injected = await _inject(key, True, str(narrow) if narrow else None)
        except AuthorizationRequired as exc:
            return {**exc.result, "tool": tool_name}
        except Exception as exc:
            return _auth_error(exc, tool_name)
        try:
            return await _call(context, kwargs, injected, handle)
        except AuthorizationRequired as exc:
            return {**exc.result, "tool": tool_name}

    managed_auth_handler.__name__ = getattr(handler, "__name__", tool_name)
    managed_auth_handler.__wrapped__ = handler  # type: ignore[attr-defined]
    return managed_auth_handler


def _auth_error(exc: BaseException, tool_name: str) -> dict[str, Any]:
    """A credential could not be resolved: a structured result, never a secret."""
    from device_code_auth import DeviceAuthError

    if isinstance(exc, DeviceAuthError):
        return {"ok": False, "status": exc.status, "error": str(exc), "tool": tool_name}
    print(f"[code_auth] {tool_name}: credential unavailable: {type(exc).__name__}: {exc}", flush=True)
    traceback.print_exc()
    return {"ok": False, "status": "credential_error", "error": str(exc), "tool": tool_name}

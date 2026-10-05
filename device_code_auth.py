"""
device_code_auth.py — OAuth 2.0 device authorization grant (RFC 8628) for code providers.

A code provider opts in with a top-level ``auth:`` block of ``type: device_code``
(see ``code_auth``).  This module owns everything after that:

  * starting a sign-in: POST to the device authorization endpoint, trying the
    configured client id(s) in order, and publishing ``verification_uri`` +
    ``user_code`` to the UI's pending area (``pending_device_auth``, plus the
    link in ``rest_provider.pending_rest_auth`` so the existing banner shows it);
  * a background poller (daemon thread) that waits out ``authorization_pending``
    / ``slow_down``, stops on ``expired_token`` / ``access_denied``, and persists
    the token once the user approves;
  * silent refresh with refresh-token rotation, for one or more *resources*
    from a single sign-in (one access token per resource scope set, the newest
    refresh token persisted), and a separate per-resource sign-in when the
    first redemption fails with a consent error;
  * an on-disk cache under ``REST_AUTH_DIR/<provider>.device.json``, optionally
    encrypted at rest with AES-256-GCM under a key derived (HKDF-SHA256, per-file
    salt) from a caller-supplied secret, with a verifier so a wrong key gives a
    clean ``key_mismatch`` error.

Secret hygiene: access/refresh tokens, device codes and the caller key are never
logged, returned, or put in an exception message.  The caller key is used only
inside the call that supplied it: a background poller receives the *derived*
encryption material for the cache file, never the key itself, and drops it
when the flow ends.  ``user_code`` is not a credential (the user must type it
at the provider's page) and is shown in the UI and tool results on purpose.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable

import httpx

from config import REST_AUTH_DIR, refresh_env

# Public view of in-flight sign-ins, keyed by provider name, for the UI to poll:
# {provider: {resource, verification_uri, verification_uri_complete, user_code,
#             expires_at, state, error}}.  Never holds a device code or a token.
pending_device_auth: dict[str, dict[str, Any]] = {}

# Seconds of slack subtracted from an access token's lifetime.
EXPIRY_SKEW = 60.0

# Default timeout (seconds) for token and device-code requests.
HTTP_TIMEOUT = float(os.environ.get("MCPPROXY_REST_TIMEOUT", "30"))

# Errors from the token endpoint that mean "this resource needs its own consent",
# so a separate per-resource sign-in is started.  Providers extend these with
# ``consent_errors`` (matched against ``error`` and ``error_description``) and
# ``consent_error_codes`` (matched against a numeric ``error_codes`` list).
DEFAULT_CONSENT_ERRORS = ("consent_required", "interaction_required")

_DECLINED_ERRORS = ("access_denied", "authorization_declined")

_CACHE_VERSION = 1
_HKDF_INFO = b"mcpproxy-device-cache-v1"

# Guards flows and cache reads/writes.  A threading lock: the poller runs on
# plain daemon threads and tool calls reach the store via asyncio.to_thread.
_lock = threading.RLock()

# In-flight sign-ins keyed by (provider, resource).  Holds the device code and,
# for an encrypted cache, the derived key material, so it is never returned.
_flows: dict[tuple[str, str], dict[str, Any]] = {}


class DeviceAuthError(Exception):
    """A device-code auth failure with a machine-readable ``status``.

    ``status`` is one of: ``no_credential`` (encryption key missing),
    ``key_mismatch``, ``cache_unreadable``, ``cache_encrypted`` (cache is
    encrypted but the provider no longer configures a key), ``sign_in_failed``,
    ``token_error``, ``missing_dependency``, ``config_error``.  Messages never
    contain a secret.
    """

    def __init__(self, message: str, status: str) -> None:
        super().__init__(message)
        self.status = status


class DeviceAuthNeeded(Exception):
    """No usable token for ``resource``; a sign-in must be started (or awaited)."""

    def __init__(self, resource: str, reason: str = "") -> None:
        super().__init__(reason or f"sign-in required for {resource}")
        self.resource = resource
        self.reason = reason


# ---------------------------------------------------------------------------
# Test seams
# ---------------------------------------------------------------------------

def _http_post(url: str, data: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """POST a form and return ``(status, json_body)``; tests replace this."""
    with httpx.Client(timeout=HTTP_TIMEOUT) as client:
        resp = client.post(url, data=data)
    try:
        body = resp.json()
    except ValueError:
        body = {}
    return resp.status_code, body if isinstance(body, dict) else {}


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _spawn(target: Callable[[], None]) -> None:
    """Run the poller; tests replace this to run it inline."""
    threading.Thread(target=target, daemon=True, name="device-code-poller").start()


# ---------------------------------------------------------------------------
# Encryption at rest
# ---------------------------------------------------------------------------

def _crypto():
    """Import ``cryptography`` only when a provider actually encrypts."""
    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    except ImportError as exc:
        raise DeviceAuthError(
            "Encrypting the device-code token cache requires the 'cryptography' "
            "package (pip install cryptography).",
            "missing_dependency",
        ) from exc
    return hashes, AESGCM, HKDF


class _CacheCipher:
    """Derived AES-256-GCM material for one cache file (one salt).

    Built from the caller key plus the file's salt; holds only the derived
    keys, never the caller key.  The salt is chosen when the file is first
    created and kept across rewrites (each write uses a fresh nonce), so a
    background poller can write the file with this object after the request
    that supplied the key has returned.
    """

    __slots__ = ("provider", "salt", "_enc_key", "_verifier")

    def __init__(self, provider: str, salt: bytes, enc_key: bytes, verifier: str) -> None:
        self.provider = provider
        self.salt = salt
        self._enc_key = enc_key
        self._verifier = verifier

    def __repr__(self) -> str:  # never show key material
        return f"<_CacheCipher provider={self.provider!r}>"

    @classmethod
    def derive(cls, provider: str, key: str, salt: bytes) -> "_CacheCipher":
        hashes, _aes, HKDF = _crypto()
        material = HKDF(
            algorithm=hashes.SHA256(), length=64, salt=salt, info=_HKDF_INFO
        ).derive(key.encode("utf-8"))
        enc_key, mac_key = material[:32], material[32:]
        verifier = base64.b64encode(
            hmac.new(mac_key, b"mcpproxy-device-cache-verifier", hashlib.sha256).digest()
        ).decode("ascii")
        return cls(provider, salt, enc_key, verifier)

    def matches(self, blob: dict[str, Any]) -> bool:
        return hmac.compare_digest(self._verifier, str(blob.get("verifier") or ""))

    def encrypt(self, state: dict[str, Any]) -> dict[str, Any]:
        _hashes, AESGCM, _hkdf = _crypto()
        nonce = os.urandom(12)
        ciphertext = AESGCM(self._enc_key).encrypt(
            nonce, json.dumps(state).encode("utf-8"), self.provider.encode("utf-8")
        )
        return {
            "v": _CACHE_VERSION,
            "kdf": "HKDF-SHA256",
            "cipher": "AES-256-GCM",
            "salt": base64.b64encode(self.salt).decode("ascii"),
            "verifier": self._verifier,
            "nonce": base64.b64encode(nonce).decode("ascii"),
            "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
        }

    def decrypt(self, blob: dict[str, Any]) -> dict[str, Any]:
        _hashes, AESGCM, _hkdf = _crypto()
        plaintext = AESGCM(self._enc_key).decrypt(
            base64.b64decode(blob["nonce"]),
            base64.b64decode(blob["ciphertext"]),
            self.provider.encode("utf-8"),
        )
        return json.loads(plaintext)


def _is_encrypted_blob(blob: dict[str, Any]) -> bool:
    return isinstance(blob, dict) and "ciphertext" in blob


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

def _scope_string(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return " ".join(str(v) for v in value if v)
    return str(value or "").strip()


class DeviceCodeStore:
    """Device-code sign-in, refresh and cache for one provider.

    Synchronous and thread-safe; async callers use ``asyncio.to_thread``.
    Every public method that needs to read an encrypted cache takes the caller
    ``key`` for that call only; nothing here keeps it.
    """

    def __init__(self, provider: str, cfg: dict[str, Any]) -> None:
        self.provider = provider
        self.cfg = cfg
        resources = cfg.get("resources") or {}
        if not isinstance(resources, dict) or not resources:
            raise DeviceAuthError(
                f"Provider '{provider}': device_code auth needs a 'resources' mapping",
                "config_error",
            )
        self.resources: dict[str, dict[str, str]] = {}
        for name, rcfg in resources.items():
            if isinstance(rcfg, dict):
                scopes = _scope_string(rcfg.get("scopes"))
                login = _scope_string(rcfg.get("login_scopes")) or scopes
            else:
                scopes = login = _scope_string(rcfg)
            self.resources[str(name)] = {"scopes": scopes, "login_scopes": login}
        self.default_resource = str(cfg.get("default_resource") or next(iter(self.resources)))
        if self.default_resource not in self.resources:
            raise DeviceAuthError(
                f"Provider '{provider}': default_resource '{self.default_resource}' "
                "is not one of its resources",
                "config_error",
            )
        self.encrypted = bool(cfg.get("encrypt_with_secret"))

    # ── configuration ───────────────────────────────────────────────────────

    def cache_path(self) -> Path:
        override = str(self.cfg.get("cache_file") or "").strip()
        if override:
            return Path(override)
        return REST_AUTH_DIR / f"{self.provider}.device.json"

    def _tenant(self) -> str:
        env_name = str(self.cfg.get("tenant_env") or "").strip()
        if env_name:
            refresh_env()
            value = (os.environ.get(env_name) or "").strip()
            if value:
                return value
        return str(self.cfg.get("tenant") or "").strip()

    def _url(self, key: str, tenant: str) -> str:
        url = str(self.cfg.get(key) or "").strip()
        if not url:
            raise DeviceAuthError(
                f"Provider '{self.provider}': auth.{key} is required", "config_error"
            )
        return url.replace("{tenant}", tenant) if "{tenant}" in url else url

    def _client_ids(self) -> list[str]:
        ids: list[str] = []
        env_name = str(self.cfg.get("client_id_env") or "").strip()
        if env_name:
            refresh_env()
            value = (os.environ.get(env_name) or "").strip()
            if value:
                ids.append(value)
        for cid in [self.cfg.get("client_id")] + list(self.cfg.get("fallback_client_ids") or []):
            cid = str(cid or "").strip()
            if cid and cid not in ids:
                ids.append(cid)
        if not ids:
            raise DeviceAuthError(
                f"Provider '{self.provider}': no client id configured (client_id, "
                "client_id_env or fallback_client_ids)",
                "config_error",
            )
        return ids

    def _extra(self) -> dict[str, str]:
        return {str(k): str(v) for k, v in (self.cfg.get("extra_token_params") or {}).items()}

    def _resource(self, resource: str | None) -> str:
        name = resource or self.default_resource
        if name not in self.resources:
            raise DeviceAuthError(
                f"Provider '{self.provider}' has no auth resource '{name}'", "config_error"
            )
        return name

    # ── cache I/O ───────────────────────────────────────────────────────────

    def _read_blob(self) -> dict[str, Any] | None:
        path = self.cache_path()
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise DeviceAuthError(
                f"The sign-in cache for '{self.provider}' is unreadable ({exc.strerror}).",
                "cache_unreadable",
            ) from None
        try:
            blob = json.loads(text)
        except ValueError:
            raise DeviceAuthError(
                f"The sign-in cache for '{self.provider}' is unreadable. Log out, then sign in again.",
                "cache_unreadable",
            ) from None
        return blob if isinstance(blob, dict) else None

    def _check_key(self, key: str | None) -> str:
        key = str(key or "").strip()
        if not key:
            raise DeviceAuthError(
                f"Provider '{self.provider}' encrypts its sign-in with a caller key, "
                "and none was supplied for this call.",
                "no_credential",
            )
        return key

    def _cipher_for(self, key: str | None, blob: dict[str, Any] | None) -> _CacheCipher | None:
        """Derived material matching ``blob`` (or a fresh salt when there is none).

        Raises ``key_mismatch`` when ``blob`` is encrypted under another key.
        """
        if not self.encrypted:
            return None
        key = self._check_key(key)
        if blob is not None and _is_encrypted_blob(blob):
            try:
                salt = base64.b64decode(blob["salt"])
            except Exception:
                raise DeviceAuthError(
                    f"The sign-in cache for '{self.provider}' is unreadable. Log out, then sign in again.",
                    "cache_unreadable",
                ) from None
            cipher = _CacheCipher.derive(self.provider, key, salt)
            if not cipher.matches(blob):
                raise self._mismatch()
            return cipher
        return _CacheCipher.derive(self.provider, key, os.urandom(16))

    def _mismatch(self) -> DeviceAuthError:
        return DeviceAuthError(
            f"This caller key does not match the stored sign-in for '{self.provider}'. "
            "Log out, then sign in again.",
            "key_mismatch",
        )

    def _open(self, blob: dict[str, Any] | None, cipher: _CacheCipher | None) -> dict[str, Any] | None:
        if blob is None:
            return None
        if _is_encrypted_blob(blob):
            if cipher is None:
                raise DeviceAuthError(
                    f"The sign-in cache for '{self.provider}' is encrypted, but the provider "
                    "no longer configures encrypt_with_secret. Log out, then sign in again.",
                    "cache_encrypted",
                )
            try:
                return cipher.decrypt(blob)
            except DeviceAuthError:
                raise
            except Exception:
                raise self._mismatch() from None
        return blob.get("state") if isinstance(blob.get("state"), dict) else None

    def _write(self, state: dict[str, Any], cipher: _CacheCipher | None) -> None:
        blob = cipher.encrypt(state) if cipher is not None else {"v": _CACHE_VERSION, "state": state}
        path = self.cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(blob, fh)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)

    def _load(self, key: str | None) -> tuple[dict[str, Any] | None, _CacheCipher | None]:
        blob = self._read_blob()
        cipher = self._cipher_for(key, blob)
        return self._open(blob, cipher), cipher

    # ── tokens ──────────────────────────────────────────────────────────────

    def access_token(self, resource: str | None, key: str | None, force_refresh: bool = False) -> str:
        """Return an access token for ``resource``, refreshing silently if needed.

        Raises ``DeviceAuthNeeded`` when the user must sign in (no cache, an
        expired/revoked grant, or a consent error for this resource) and
        ``DeviceAuthError`` for everything else.
        """
        resource = self._resource(resource)
        with _lock:
            state, cipher = self._load(key)
            if not state or not state.get("refresh_tokens"):
                raise DeviceAuthNeeded(resource, "not_signed_in")
            acc = (state.get("access") or {}).get(resource) or {}
            if (
                not force_refresh
                and acc.get("token")
                and time.time() < float(acc.get("expires_at") or 0) - EXPIRY_SKEW
            ):
                return acc["token"]
            slots = state["refresh_tokens"]
            slot = resource if resource in slots else "primary"
            refresh_token = slots.get(slot)
            if not refresh_token:
                raise DeviceAuthNeeded(resource, "not_signed_in")
            tenant = state.get("tenant") or self._tenant()
            data = {
                "grant_type": "refresh_token",
                "client_id": state.get("client_id") or self._client_ids()[0],
                "refresh_token": refresh_token,
                "scope": self.resources[resource]["scopes"],
                **self._extra(),
            }
            try:
                status, body = _http_post(self._url("token_url", tenant), data)
            except Exception as exc:
                raise DeviceAuthError(
                    f"Token request for '{self.provider}' failed: {type(exc).__name__}",
                    "token_error",
                ) from None
            if status == 200 and body.get("access_token"):
                new_state = json.loads(json.dumps(state))
                new_state.setdefault("access", {})[resource] = {
                    "token": body["access_token"],
                    "expires_at": time.time() + float(body.get("expires_in") or 3600),
                }
                if body.get("refresh_token"):
                    # Rotation: a refresh token is single-use at many providers,
                    # so the newest one always replaces the slot it came from.
                    new_state["refresh_tokens"][slot] = body["refresh_token"]
                new_state["last_refresh"] = int(time.time())
                self._write(new_state, cipher)
                return body["access_token"]
            if self._is_consent_error(body):
                raise DeviceAuthNeeded(resource, "consent_required")
            if body.get("error") == "invalid_grant":
                raise DeviceAuthNeeded(resource, "expired")
            raise DeviceAuthError(
                f"Token refresh for '{self.provider}' ({resource}) failed: "
                f"{body.get('error') or 'HTTP ' + str(status)}",
                "token_error",
            )

    def _is_consent_error(self, body: dict[str, Any]) -> bool:
        names = list(DEFAULT_CONSENT_ERRORS) + [str(x) for x in (self.cfg.get("consent_errors") or [])]
        error = str(body.get("error") or "")
        desc = str(body.get("error_description") or "")
        if any(n and (n == error or n in desc) for n in names):
            return True
        codes = {int(c) for c in (self.cfg.get("consent_error_codes") or []) if str(c).isdigit()}
        try:
            returned = {int(c) for c in (body.get("error_codes") or [])}
        except (TypeError, ValueError):
            returned = set()
        return bool(codes & returned)

    # ── sign-in ─────────────────────────────────────────────────────────────

    def pending(self, resource: str | None = None) -> dict[str, Any] | None:
        """Public view of a still-pending sign-in for ``resource`` (any if None)."""
        with _lock:
            for (prov, res), flow in _flows.items():
                if prov != self.provider or (resource and res != resource):
                    continue
                if flow["state"] == "pending" and time.time() < flow["expires_at"]:
                    return _public(flow)
        return None

    def start(self, resource: str | None, key: str | None, *, reason: str = "") -> dict[str, Any]:
        """Start (or reuse) a sign-in for ``resource`` and return its public view.

        A sign-in for a resource that already has a working shared sign-in
        (reason ``consent_required``) is stored in that resource's own slot;
        any other sign-in replaces the whole cache.
        """
        resource = self._resource(resource)
        existing = self.pending(resource)
        if existing is not None:
            return existing
        kind = "resource" if reason == "consent_required" else "primary"
        with _lock:
            # Verifies the key against an existing encrypted cache (a full
            # sign-in never silently replaces a sign-in made under another key)
            # and yields the derived material the poller will write with.
            cipher = self._cipher_for(key, self._read_blob())
        tenant = self._tenant()
        url = self._url("device_authorization_url", tenant)
        scope = self.resources[resource]["login_scopes"]
        failures: list[str] = []
        for index, client_id in enumerate(self._client_ids()):
            try:
                status, payload = _http_post(url, {"client_id": client_id, "scope": scope})
            except Exception as exc:
                failures.append(f"client #{index + 1}: {type(exc).__name__}")
                continue
            if status != 200 or not payload.get("device_code"):
                failures.append(
                    f"client #{index + 1}: {payload.get('error') or 'HTTP ' + str(status)}"
                )
                continue
            flow = {
                "provider": self.provider,
                "resource": resource,
                "kind": kind,
                "reason": reason,
                "client_id": client_id,
                "tenant": tenant,
                "device_code": payload["device_code"],
                "user_code": payload.get("user_code", ""),
                "verification_uri": payload.get("verification_uri")
                or payload.get("verification_url")
                or "",
                "verification_uri_complete": payload.get("verification_uri_complete") or "",
                "interval": max(1, int(payload.get("interval") or 5)),
                "expires_at": time.time() + int(
                    payload["expires_in"] if payload.get("expires_in") is not None else 900
                ),
                "message": payload.get("message") or "",
                "state": "pending",
                "error": None,
                "cancelled": False,
                "cipher": cipher,
            }
            with _lock:
                old = _flows.get((self.provider, resource))
                if old is not None:
                    old["cancelled"] = True
                _flows[(self.provider, resource)] = flow
                _publish(flow)
            print(
                f"[device_code_auth] sign-in started for provider '{self.provider}' "
                f"(resource '{resource}'); the code is shown in the mcpproxy UI.",
                flush=True,
            )
            _spawn(lambda: self._poll(flow))
            return _public(flow)
        raise DeviceAuthError(
            f"No client could start a sign-in for '{self.provider}': " + "; ".join(failures),
            "sign_in_failed",
        )

    def _poll(self, flow: dict[str, Any]) -> None:
        """Poll the token endpoint until the user finishes (runs on a daemon thread)."""
        try:
            interval = flow["interval"]
            url = self._url("token_url", flow["tenant"])
            while True:
                if flow["cancelled"]:
                    return
                if time.time() >= flow["expires_at"]:
                    self._finish(flow, "expired", "the code expired before sign-in completed")
                    return
                _sleep(interval)
                if flow["cancelled"]:
                    return
                try:
                    status, body = _http_post(url, {
                        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                        "client_id": flow["client_id"],
                        "device_code": flow["device_code"],
                        **self._extra(),
                    })
                except Exception as exc:  # network blip: keep polling until expiry
                    print(
                        f"[device_code_auth:_poll] {self.provider}: poll failed "
                        f"({type(exc).__name__}); retrying",
                        flush=True,
                    )
                    continue
                if status == 200 and body.get("access_token"):
                    self._complete(flow, body)
                    return
                error = str(body.get("error") or "")
                if error == "authorization_pending":
                    continue
                if error == "slow_down":
                    interval += 5
                    continue
                if error == "expired_token":
                    self._finish(flow, "expired", "the code expired before sign-in completed")
                elif error in _DECLINED_ERRORS:
                    self._finish(flow, "declined", "the sign-in was declined")
                else:
                    self._finish(flow, "failed", error or f"HTTP {status}")
                return
        except Exception as exc:  # noqa: BLE001 — a poller must never crash silently
            print(f"[device_code_auth:_poll] {self.provider}: {type(exc).__name__}", flush=True)
            traceback.print_exc()
            self._finish(flow, "failed", type(exc).__name__)

    def _complete(self, flow: dict[str, Any], body: dict[str, Any]) -> None:
        with _lock:
            if flow["cancelled"]:
                return
            cipher = flow["cipher"]
            resource = flow["resource"]
            access = {
                "token": body["access_token"],
                "expires_at": time.time() + float(body.get("expires_in") or 3600),
            }
            refresh = body.get("refresh_token") or ""
            try:
                blob = self._read_blob()
            except DeviceAuthError:
                blob = None
            if (
                blob is not None
                and cipher is not None
                and _is_encrypted_blob(blob)
                and not cipher.matches(blob)
            ):
                # The cache was replaced (logout and a new sign-in) while this
                # flow waited; never overwrite someone else's sign-in.
                self._finish(flow, "failed", "the stored sign-in changed while waiting")
                return
            current = None
            if flow["kind"] == "resource" and blob is not None:
                try:
                    current = self._open(blob, cipher)
                except DeviceAuthError:
                    current = None
            if current:
                state = json.loads(json.dumps(current))
                if refresh:
                    state.setdefault("refresh_tokens", {})[resource] = refresh
                state.setdefault("access", {})[resource] = access
            else:
                slot = resource if flow["kind"] == "resource" else "primary"
                state = {
                    "tenant": flow["tenant"],
                    "client_id": flow["client_id"],
                    "refresh_tokens": {slot: refresh} if refresh else {},
                    "access": {resource: access},
                    "signed_in_at": int(time.time()),
                }
            try:
                self._write(state, cipher)
            except Exception as exc:
                print(
                    f"[device_code_auth:_complete] {self.provider}: could not write the "
                    f"sign-in cache ({type(exc).__name__})",
                    flush=True,
                )
                traceback.print_exc()
                self._finish(flow, "failed", "could not write the sign-in cache")
                return
            self._finish(flow, "completed", None)
        print(
            f"[device_code_auth] sign-in completed for provider '{self.provider}' "
            f"(resource '{resource}').",
            flush=True,
        )

    def _finish(self, flow: dict[str, Any], state: str, error: str | None) -> None:
        with _lock:
            flow["state"] = state
            flow["error"] = error
            flow["cipher"] = None          # drop derived key material
            flow["device_code"] = ""
            if _flows.get((self.provider, flow["resource"])) is flow:
                _unpublish(self.provider, flow["resource"])
        if state not in ("completed",):
            print(
                f"[device_code_auth] sign-in for provider '{self.provider}' "
                f"(resource '{flow['resource']}') ended: {state}",
                flush=True,
            )

    def last_flow(self, resource: str | None = None) -> dict[str, Any] | None:
        with _lock:
            flow = _flows.get((self.provider, self._resource(resource)))
            return _public(flow) if flow else None

    # ── status / logout ─────────────────────────────────────────────────────

    def status(self, key: str | None = None) -> dict[str, Any]:
        """Describe the sign-in without revealing anything secret.  Never raises."""
        out: dict[str, Any] = {
            "provider": self.provider,
            "type": "device_code",
            "encrypted": self.encrypted,
            "cache_present": self.cache_path().exists(),
            "signed_in": None,
            "resources": {},
            "pending": [],
        }
        with _lock:
            for (prov, _res), flow in _flows.items():
                if prov == self.provider and flow["state"] == "pending" and time.time() < flow["expires_at"]:
                    out["pending"].append(_public(flow))
            if not out["cache_present"]:
                out["signed_in"] = False
                return out
            if self.encrypted and not str(key or "").strip():
                out["note"] = "Supply the caller key to see token details."
                return out
            try:
                state, _cipher = self._load(key)
            except DeviceAuthError as exc:
                out["error"] = str(exc)
                out["error_status"] = exc.status
                return out
        state = state or {}
        out["signed_in"] = bool(state.get("refresh_tokens"))
        now = time.time()
        for name in self.resources:
            acc = (state.get("access") or {}).get(name) or {}
            out["resources"][name] = {
                "has_access_token": bool(acc.get("token")),
                "expires_in": max(0, int(float(acc.get("expires_at") or 0) - now)) if acc.get("token") else 0,
                "own_refresh_slot": name in (state.get("refresh_tokens") or {}),
            }
        if state.get("signed_in_at"):
            out["signed_in_at"] = state["signed_in_at"]
        return out

    def logout(self) -> dict[str, Any]:
        """Cancel sign-ins and delete the cache.  Needs no key, so it works with a wrong one."""
        with _lock:
            for (prov, res), flow in list(_flows.items()):
                if prov == self.provider:
                    flow["cancelled"] = True
                    flow["cipher"] = None
                    flow["device_code"] = ""
                    _flows.pop((prov, res), None)
            _unpublish(self.provider, None)
            path = self.cache_path()
            removed = False
            for candidate in (path, path.with_name(path.name + ".tmp")):
                try:
                    candidate.unlink()
                    removed = removed or candidate == path
                except FileNotFoundError:
                    pass
        print(f"[device_code_auth] signed out provider '{self.provider}'.", flush=True)
        return {"ok": True, "removed_cache": removed}


# ---------------------------------------------------------------------------
# Pending-area publication
# ---------------------------------------------------------------------------

def _public(flow: dict[str, Any]) -> dict[str, Any]:
    """The parts of a flow that may be shown to the user (no device code, no keys)."""
    return {
        "provider": flow["provider"],
        "resource": flow["resource"],
        "verification_uri": flow["verification_uri"],
        "verification_uri_complete": flow["verification_uri_complete"],
        "user_code": flow["user_code"],
        "expires_in": max(0, int(flow["expires_at"] - time.time())),
        "interval": flow["interval"],
        "message": flow["message"],
        "state": flow["state"],
        "error": flow["error"],
    }


def _publish(flow: dict[str, Any]) -> None:
    from rest_provider import pending_rest_auth

    view = _public(flow)
    view["expires_at"] = flow["expires_at"]
    pending_device_auth[flow["provider"]] = view
    link = flow["verification_uri_complete"] or flow["verification_uri"]
    if link:
        pending_rest_auth[flow["provider"]] = link


def _unpublish(provider: str, resource: str | None) -> None:
    from rest_provider import pending_rest_auth

    current = pending_device_auth.get(provider)
    if current is None or resource is None or current.get("resource") == resource:
        pending_device_auth.pop(provider, None)
        pending_rest_auth.pop(provider, None)


def public_pending() -> dict[str, dict[str, Any]]:
    """Pending sign-ins for the UI, with ``expires_in`` recomputed; expired ones dropped."""
    now = time.time()
    out: dict[str, dict[str, Any]] = {}
    with _lock:
        for provider, view in list(pending_device_auth.items()):
            remaining = int(view.get("expires_at", 0) - now)
            if remaining <= 0:
                continue
            item = {k: v for k, v in view.items() if k != "expires_at"}
            item["expires_in"] = remaining
            out[provider] = item
    return out


def reset_state() -> None:
    """Forget all flows and pending entries (tests only)."""
    with _lock:
        for flow in _flows.values():
            flow["cancelled"] = True
        _flows.clear()
        pending_device_auth.clear()

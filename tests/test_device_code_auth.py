"""Tests for built-in device-code sign-in (``device_code_auth``) and its wiring.

A fake identity provider stands in for the device-authorization and token
endpoints (``device_code_auth._http_post``); the poller runs inline
(``_spawn``) with a no-op ``_sleep`` that records each wait.  Covers start and
publication, polling outcomes (pending, slow_down, expired, declined),
persistence across a simulated restart, refresh rotation, several resources
from one sign-in, the consent-error per-resource sign-in, fallback client ids,
encryption at rest (no plaintext, wrong key, missing key, logout), that the
caller key never outlives its call, and the lazy ``cryptography`` import.
"""
import json
import time
from pathlib import Path

import pytest

import code_auth
import device_code_auth as dca
import rest_provider
import server
from device_code_auth import DeviceAuthError, DeviceAuthNeeded, DeviceCodeStore

CALLER_KEY = "caller-key-value-XYZ-0001"
OTHER_KEY = "other-caller-key-value-0002"

API_SCOPES = "https://api.example.test/.default offline_access"
NOTES_SCOPES = "https://notes.example.test/Notes.ReadWrite offline_access"


def _cfg(**extra) -> dict:
    cfg = {
        "type": "device_code",
        "inject_as": {"api": "api_token", "notes": "notes_token"},
        "device_authorization_url": "https://login.example.test/{tenant}/devicecode",
        "token_url": "https://login.example.test/{tenant}/token",
        "tenant": "organizations",
        "client_id": "client-one",
        "default_resource": "api",
        "resources": {
            "api": {"scopes": API_SCOPES, "login_scopes": "User.Read offline_access"},
            "notes": {"scopes": NOTES_SCOPES},
        },
        "consent_error_codes": [65001],
    }
    cfg.update(extra)
    return cfg


class FakeIdP:
    """Device-code + token endpoints with scripted behaviour and a call log."""

    def __init__(self):
        self.calls: list[dict] = []
        self.device_status: dict[str, tuple[int, dict]] = {}
        self.poll_script: list[tuple[int, dict]] = []
        self.refresh_failures: dict[str, tuple[int, dict]] = {}  # scope -> response
        self.n = 0
        self.issued: list[str] = []

    def _token(self, prefix: str) -> str:
        self.n += 1
        value = f"{prefix}-secret-{self.n:04d}-ZZZZ"
        self.issued.append(value)
        return value

    def post(self, url: str, data: dict) -> tuple[int, dict]:
        self.calls.append({"url": url, "data": dict(data)})
        if url.endswith("/devicecode"):
            status = self.device_status.get(data["client_id"])
            if status:
                return status
            return 200, {
                "device_code": self._token("devicecode"),
                "user_code": f"USER-{self.n}",
                "verification_uri": "https://login.example.test/device",
                "expires_in": 900,
                "interval": 5,
            }
        grant = data.get("grant_type")
        if grant == "urn:ietf:params:oauth:grant-type:device_code":
            if self.poll_script:
                return self.poll_script.pop(0)
            return 200, {"access_token": self._token("access"), "refresh_token": self._token("refresh"),
                         "expires_in": 3600}
        if grant == "refresh_token":
            failure = self.refresh_failures.get(data.get("scope"))
            if failure:
                return failure
            return 200, {"access_token": self._token("access"), "refresh_token": self._token("refresh"),
                         "expires_in": 3600}
        return 400, {"error": "unsupported_grant_type"}

    def refreshes(self) -> list[dict]:
        return [c["data"] for c in self.calls if c["data"].get("grant_type") == "refresh_token"]


@pytest.fixture()
def idp(monkeypatch):
    fake = FakeIdP()
    monkeypatch.setattr(dca, "_http_post", fake.post)
    return fake


@pytest.fixture()
def sleeps(monkeypatch):
    waited: list[float] = []
    monkeypatch.setattr(dca, "_sleep", waited.append)
    return waited


@pytest.fixture()
def inline(monkeypatch):
    """Run the poller synchronously when a sign-in starts."""
    monkeypatch.setattr(dca, "_spawn", lambda target: target())


@pytest.fixture()
def deferred(monkeypatch):
    """Capture poller targets so a test can run them later (user still busy)."""
    targets: list = []
    monkeypatch.setattr(dca, "_spawn", targets.append)
    return targets


@pytest.fixture()
def auth_dir(tmp_path, monkeypatch):
    d = tmp_path / "rest-auth"
    monkeypatch.setattr(dca, "REST_AUTH_DIR", d)
    monkeypatch.setattr(rest_provider, "REST_AUTH_DIR", d)
    return d


@pytest.fixture(autouse=True)
def _reset():
    dca.reset_state()
    rest_provider.pending_rest_auth.clear()
    code_auth._providers.clear()
    yield
    dca.reset_state()
    rest_provider.pending_rest_auth.clear()
    code_auth._providers.clear()


# ---------------------------------------------------------------------------
# Start + publication
# ---------------------------------------------------------------------------

class TestStart:
    def test_start_publishes_pending_without_secrets(self, idp, deferred, auth_dir):
        store = DeviceCodeStore("prov", _cfg())
        view = store.start(None, None)
        assert view["user_code"] == "USER-1"
        assert view["verification_uri"] == "https://login.example.test/device"
        assert view["resource"] == "api"
        assert "device_code" not in view
        assert idp.calls[0]["url"] == "https://login.example.test/organizations/devicecode"
        assert idp.calls[0]["data"] == {"client_id": "client-one", "scope": "User.Read offline_access"}
        pending = dca.public_pending()["prov"]
        assert pending["user_code"] == "USER-1" and 0 < pending["expires_in"] <= 900
        assert rest_provider.pending_rest_auth["prov"] == "https://login.example.test/device"
        assert all("devicecode-secret" not in str(v) for v in pending.values())

    def test_second_start_reuses_the_pending_flow(self, idp, deferred, auth_dir):
        store = DeviceCodeStore("prov", _cfg())
        first = store.start(None, None)
        second = store.start(None, None)
        assert first["user_code"] == second["user_code"]
        assert len([c for c in idp.calls if c["url"].endswith("/devicecode")]) == 1

    def test_fallback_client_ids(self, idp, inline, sleeps, auth_dir):
        idp.device_status["client-one"] = (400, {"error": "unauthorized_client"})
        store = DeviceCodeStore("prov", _cfg(fallback_client_ids=["client-two"]))
        store.start(None, None)
        assert [c["data"]["client_id"] for c in idp.calls if c["url"].endswith("/devicecode")] == [
            "client-one", "client-two"]
        store.access_token("api", None, force_refresh=True)
        assert idp.refreshes()[-1]["client_id"] == "client-two"

    def test_client_id_env_wins(self, idp, deferred, auth_dir, monkeypatch):
        monkeypatch.setenv("DEMO_DEVICE_CLIENT", "client-from-env")
        DeviceCodeStore("prov", _cfg(client_id_env="DEMO_DEVICE_CLIENT")).start(None, None)
        assert idp.calls[0]["data"]["client_id"] == "client-from-env"

    def test_all_clients_failing_is_a_clean_error(self, idp, deferred, auth_dir):
        idp.device_status["client-one"] = (400, {"error": "invalid_client"})
        with pytest.raises(DeviceAuthError) as info:
            DeviceCodeStore("prov", _cfg()).start(None, None)
        assert info.value.status == "sign_in_failed"
        assert "invalid_client" in str(info.value)


# ---------------------------------------------------------------------------
# Polling
# ---------------------------------------------------------------------------

class TestPolling:
    def test_pending_then_slow_down_then_success(self, idp, inline, sleeps, auth_dir):
        idp.poll_script = [
            (400, {"error": "authorization_pending"}),
            (400, {"error": "slow_down"}),
            (400, {"error": "authorization_pending"}),
        ]
        store = DeviceCodeStore("prov", _cfg())
        store.start(None, None)
        assert sleeps == [5, 5, 10, 10]  # slow_down adds 5 seconds
        assert store.last_flow()["state"] == "completed"
        assert dca.public_pending() == {} and "prov" not in rest_provider.pending_rest_auth
        assert store.access_token("api", None).startswith("access-secret-")

    def test_expired_token(self, idp, inline, sleeps, auth_dir):
        idp.poll_script = [(400, {"error": "expired_token"})]
        store = DeviceCodeStore("prov", _cfg())
        store.start(None, None)
        assert store.last_flow()["state"] == "expired"
        assert dca.public_pending() == {}
        assert not store.cache_path().exists()

    def test_local_expiry_stops_polling(self, idp, inline, sleeps, auth_dir, monkeypatch):
        real_post = idp.post

        def short_lived(url, data):
            status, body = real_post(url, data)
            if url.endswith("/devicecode"):
                body = {**body, "expires_in": 0}
            return status, body

        monkeypatch.setattr(dca, "_http_post", short_lived)
        store = DeviceCodeStore("prov", _cfg())
        store.start(None, None)
        assert store.last_flow()["state"] == "expired"
        assert sleeps == []

    @pytest.mark.parametrize("error", ["access_denied", "authorization_declined"])
    def test_declined(self, idp, inline, sleeps, auth_dir, error):
        idp.poll_script = [(400, {"error": error})]
        store = DeviceCodeStore("prov", _cfg())
        store.start(None, None)
        assert store.last_flow()["state"] == "declined"
        assert dca.public_pending() == {}

    def test_unknown_error_fails(self, idp, inline, sleeps, auth_dir):
        idp.poll_script = [(400, {"error": "invalid_grant"})]
        store = DeviceCodeStore("prov", _cfg())
        store.start(None, None)
        assert store.last_flow()["state"] == "failed"

    def test_network_errors_keep_polling(self, idp, inline, sleeps, auth_dir, monkeypatch):
        real_post = idp.post
        failures = {"n": 0}

        def flaky(url, data):
            if data.get("grant_type", "").endswith("device_code") and failures["n"] == 0:
                failures["n"] += 1
                raise ConnectionError("boom")
            return real_post(url, data)

        monkeypatch.setattr(dca, "_http_post", flaky)
        store = DeviceCodeStore("prov", _cfg())
        store.start(None, None)
        assert store.last_flow()["state"] == "completed"


# ---------------------------------------------------------------------------
# Tokens: persistence, rotation, resources, consent
# ---------------------------------------------------------------------------

class TestTokens:
    def test_persists_across_restart(self, idp, inline, sleeps, auth_dir):
        DeviceCodeStore("prov", _cfg()).start(None, None)
        token = DeviceCodeStore("prov", _cfg()).access_token("api", None)
        dca.reset_state()  # simulated restart: in-memory flows are gone
        calls_before = len(idp.calls)
        assert DeviceCodeStore("prov", _cfg()).access_token("api", None) == token
        assert len(idp.calls) == calls_before  # served from the cache

    def test_not_signed_in(self, idp, auth_dir):
        with pytest.raises(DeviceAuthNeeded) as info:
            DeviceCodeStore("prov", _cfg()).access_token("api", None)
        assert info.value.reason == "not_signed_in"

    def test_refresh_rotation(self, idp, inline, sleeps, auth_dir):
        store = DeviceCodeStore("prov", _cfg())
        store.start(None, None)
        first_refresh = json.loads(store.cache_path().read_text())["state"]["refresh_tokens"]["primary"]
        store.access_token("api", None, force_refresh=True)
        assert idp.refreshes()[-1]["refresh_token"] == first_refresh
        second_refresh = json.loads(store.cache_path().read_text())["state"]["refresh_tokens"]["primary"]
        assert second_refresh != first_refresh
        store.access_token("api", None, force_refresh=True)
        assert idp.refreshes()[-1]["refresh_token"] == second_refresh

    def test_multiple_resources_from_one_sign_in(self, idp, inline, sleeps, auth_dir):
        store = DeviceCodeStore("prov", _cfg(extra_token_params={"client_info": "1"}))
        store.start(None, None)
        api = store.access_token("api", None)
        notes = store.access_token("notes", None)
        assert api != notes
        last = idp.refreshes()[-1]
        assert last["scope"] == NOTES_SCOPES and last["client_info"] == "1"
        state = json.loads(store.cache_path().read_text())["state"]
        assert set(state["access"]) == {"api", "notes"}
        assert state["refresh_tokens"] == {"primary": idp.issued[-1]}  # newest persisted
        # Both are now cached: no further token requests.
        n = len(idp.calls)
        assert store.access_token("api", None) == api and store.access_token("notes", None) == notes
        assert len(idp.calls) == n

    def test_invalid_grant_means_sign_in_again(self, idp, inline, sleeps, auth_dir):
        store = DeviceCodeStore("prov", _cfg())
        store.start(None, None)
        idp.refresh_failures[API_SCOPES] = (400, {"error": "invalid_grant"})
        with pytest.raises(DeviceAuthNeeded) as info:
            store.access_token("api", None, force_refresh=True)
        assert info.value.reason == "expired"

    def test_consent_error_starts_a_per_resource_sign_in(self, idp, inline, sleeps, auth_dir):
        store = DeviceCodeStore("prov", _cfg())
        store.start(None, None)
        primary = json.loads(store.cache_path().read_text())["state"]["refresh_tokens"]["primary"]
        idp.refresh_failures[NOTES_SCOPES] = (400, {"error": "invalid_grant", "error_codes": [65001]})
        with pytest.raises(DeviceAuthNeeded) as info:
            store.access_token("notes", None)
        assert info.value.reason == "consent_required"

        view = store.start("notes", None, reason="consent_required")
        assert view["resource"] == "notes"
        assert idp.calls[-2]["data"]["scope"] == NOTES_SCOPES  # login scopes default to scopes
        state = json.loads(store.cache_path().read_text())["state"]
        assert state["refresh_tokens"]["primary"] == primary  # shared sign-in kept
        assert "notes" in state["refresh_tokens"]
        del idp.refresh_failures[NOTES_SCOPES]
        store.access_token("notes", None, force_refresh=True)
        assert idp.refreshes()[-1]["refresh_token"] == state["refresh_tokens"]["notes"]

    def test_consent_errors_by_name(self, idp, inline, sleeps, auth_dir):
        store = DeviceCodeStore("prov", _cfg(consent_errors=["AADSTS65001"]))
        store.start(None, None)
        idp.refresh_failures[NOTES_SCOPES] = (400, {"error": "invalid_grant",
                                                    "error_description": "AADSTS65001: no consent"})
        with pytest.raises(DeviceAuthNeeded) as info:
            store.access_token("notes", None)
        assert info.value.reason == "consent_required"


# ---------------------------------------------------------------------------
# Encryption at rest
# ---------------------------------------------------------------------------

class TestEncryption:
    def _signed_in(self, idp, cfg=None):
        store = DeviceCodeStore("prov", cfg or _cfg(encrypt_with_secret="caller_key"))
        store.start(None, CALLER_KEY)
        return store

    def test_no_plaintext_tokens_on_disk(self, idp, inline, sleeps, auth_dir):
        store = self._signed_in(idp)
        store.access_token("notes", CALLER_KEY)
        raw = store.cache_path().read_bytes()
        blob = json.loads(raw)
        assert {"v", "kdf", "cipher", "salt", "verifier", "nonce", "ciphertext"} <= set(blob)
        assert blob["cipher"] == "AES-256-GCM" and blob["kdf"] == "HKDF-SHA256"
        for token in idp.issued:
            assert token.encode() not in raw
        assert CALLER_KEY.encode() not in raw
        assert (store.cache_path().stat().st_mode & 0o777) == 0o600

    def test_salt_is_per_file_and_nonce_per_write(self, idp, inline, sleeps, auth_dir):
        store = self._signed_in(idp)
        first = json.loads(store.cache_path().read_text())
        store.access_token("api", CALLER_KEY, force_refresh=True)
        second = json.loads(store.cache_path().read_text())
        assert first["salt"] == second["salt"]
        assert first["nonce"] != second["nonce"]
        store.logout()
        third_store = self._signed_in(idp)
        assert json.loads(third_store.cache_path().read_text())["salt"] != first["salt"]

    def test_round_trip_with_the_right_key(self, idp, inline, sleeps, auth_dir):
        store = self._signed_in(idp)
        token = store.access_token("api", CALLER_KEY)
        assert DeviceCodeStore("prov", _cfg(encrypt_with_secret="caller_key")).access_token("api", CALLER_KEY) == token

    def test_wrong_key_is_a_clean_error(self, idp, inline, sleeps, auth_dir):
        store = self._signed_in(idp)
        with pytest.raises(DeviceAuthError) as info:
            store.access_token("api", OTHER_KEY)
        assert info.value.status == "key_mismatch"
        assert OTHER_KEY not in str(info.value) and CALLER_KEY not in str(info.value)
        status = store.status(OTHER_KEY)
        assert status["error_status"] == "key_mismatch" and status["cache_present"] is True

    def test_wrong_key_cannot_replace_the_sign_in(self, idp, inline, sleeps, auth_dir):
        store = self._signed_in(idp)
        with pytest.raises(DeviceAuthError) as info:
            store.start(None, OTHER_KEY)
        assert info.value.status == "key_mismatch"

    def test_missing_key(self, idp, inline, sleeps, auth_dir):
        store = self._signed_in(idp)
        with pytest.raises(DeviceAuthError) as info:
            store.access_token("api", "")
        assert info.value.status == "no_credential"

    def test_logout_works_with_a_wrong_or_no_key(self, idp, deferred, sleeps, auth_dir):
        store = DeviceCodeStore("prov", _cfg(encrypt_with_secret="caller_key"))
        store.start(None, CALLER_KEY)
        deferred.pop()()  # complete the sign-in
        store.start("notes", CALLER_KEY, reason="consent_required")  # leave one pending
        assert dca.public_pending()
        result = store.logout()
        assert result == {"ok": True, "removed_cache": True}
        assert not store.cache_path().exists()
        assert dca.public_pending() == {} and "prov" not in rest_provider.pending_rest_auth
        # The cancelled poller does nothing when it eventually runs.
        deferred.pop()()
        assert not store.cache_path().exists()

    def test_tampered_ciphertext(self, idp, inline, sleeps, auth_dir):
        store = self._signed_in(idp)
        blob = json.loads(store.cache_path().read_text())
        blob["ciphertext"] = blob["ciphertext"][:-8] + "AAAAAAA="
        store.cache_path().write_text(json.dumps(blob))
        with pytest.raises(DeviceAuthError) as info:
            store.access_token("api", CALLER_KEY)
        assert info.value.status == "key_mismatch"


# ---------------------------------------------------------------------------
# A3: the caller key never outlives its call
# ---------------------------------------------------------------------------

class TestKeyLifetime:
    def test_flow_holds_only_derived_material(self, idp, deferred, sleeps, auth_dir, capsys):
        store = DeviceCodeStore("prov", _cfg(encrypt_with_secret="caller_key"))
        view = store.start(None, CALLER_KEY)
        flow = dca._flows[("prov", "api")]
        assert CALLER_KEY not in repr(flow) and CALLER_KEY not in repr(view)
        cipher = flow["cipher"]
        assert not any(isinstance(getattr(cipher, s), str) and CALLER_KEY in getattr(cipher, s)
                       for s in cipher.__slots__)
        # The poller completes with the derived material only.
        deferred.pop()()
        assert flow["cipher"] is None  # dropped when the flow ended
        assert store.access_token("api", CALLER_KEY)
        out = capsys.readouterr()
        for secret in [CALLER_KEY] + idp.issued:
            assert secret not in out.out and secret not in out.err


# ---------------------------------------------------------------------------
# Lazy import
# ---------------------------------------------------------------------------

class TestLazyImport:
    def test_unencrypted_never_touches_cryptography(self, idp, inline, sleeps, auth_dir, monkeypatch):
        def forbidden():
            raise AssertionError("cryptography imported for an unencrypted cache")

        monkeypatch.setattr(dca, "_crypto", forbidden)
        store = DeviceCodeStore("prov", _cfg())
        store.start(None, None)
        assert store.access_token("notes", None)

    def test_missing_dependency_is_reported(self, idp, deferred, auth_dir, monkeypatch):
        def missing():
            raise DeviceAuthError("needs cryptography", "missing_dependency")

        monkeypatch.setattr(dca, "_crypto", missing)
        with pytest.raises(DeviceAuthError) as info:
            DeviceCodeStore("prov", _cfg(encrypt_with_secret="caller_key")).start(None, CALLER_KEY)
        assert info.value.status == "missing_dependency"


# ---------------------------------------------------------------------------
# Wired through a code provider
# ---------------------------------------------------------------------------

CODE = (
    "async def me(context, caller_key, api_token=None):\n"
    "    return {'ok': True, 'api': api_token}\n"
    "async def notes(context, caller_key, notes_token=None):\n"
    "    return {'ok': True, 'notes': notes_token}\n"
)


def _tools():
    secrets = {"env": {"caller_key": "DEMO_CALLER_KEY"}, "headers": {"caller_key": "X-Demo-Key"}}
    return [
        {"name": "me", "function": "me", "description": "me", "auth_resources": ["api"],
         "input_schema": {"type": "object", "properties": {}}, "secrets": secrets},
        {"name": "notes", "function": "notes", "description": "notes", "auth_resources": ["notes"],
         "input_schema": {"type": "object", "properties": {}}, "secrets": secrets},
    ]


def _handlers(cfg):
    spec = {"code": CODE, "auth": cfg, "tools": _tools(), "_config_path": "/tmp/prov.yaml"}
    return {k.split("__", 1)[1]: v[1] for k, v in server.build_tool_handlers(spec).items()}


class TestWrapper:
    @pytest.mark.asyncio
    async def test_first_use_prompts_then_injects(self, idp, deferred, sleeps, auth_dir):
        handlers = _handlers(_cfg(encrypt_with_secret="caller_key"))
        first = await handlers["me"](context={}, caller_key=CALLER_KEY)
        assert first["status"] == "authorization_required"
        assert first["user_code"] and first["verification_uri"] and first["expires_in"] > 0
        assert first["resource"] == "api" and first["tool"] == "me"
        assert CALLER_KEY not in json.dumps(first)
        deferred.pop()()  # user finishes signing in
        second = await handlers["me"](context={}, caller_key=CALLER_KEY)
        assert second["ok"] is True and second["api"].startswith("access-secret-")
        notes = await handlers["notes"](context={}, caller_key=CALLER_KEY)
        assert notes["notes"].startswith("access-secret-") and notes["notes"] != second["api"]

    @pytest.mark.asyncio
    async def test_consent_error_returns_a_resource_prompt(self, idp, inline, sleeps, auth_dir):
        handlers = _handlers(_cfg())
        await handlers["me"](context={}, caller_key="unused")  # signs in inline
        idp.refresh_failures[NOTES_SCOPES] = (400, {"error": "invalid_grant", "error_codes": [65001]})
        result = await handlers["notes"](context={}, caller_key="unused")
        assert result["status"] == "authorization_required"
        assert result["resource"] == "notes"
        assert "own one-time approval" in result["message"]

    @pytest.mark.asyncio
    async def test_wrong_key_is_structured(self, idp, inline, sleeps, auth_dir):
        handlers = _handlers(_cfg(encrypt_with_secret="caller_key"))
        await handlers["me"](context={}, caller_key=CALLER_KEY)
        result = await handlers["me"](context={}, caller_key=OTHER_KEY)
        assert result == {"ok": False, "status": "key_mismatch", "error": result["error"], "tool": "me"}
        assert OTHER_KEY not in result["error"]

    @pytest.mark.asyncio
    async def test_401_refreshes_only_the_named_resource(self, idp, inline, sleeps, auth_dir):
        code = (
            "seen = []\n"
            "async def both(context, caller_key, api_token=None, notes_token=None):\n"
            "    seen.append((api_token, notes_token))\n"
            "    if len(seen) == 1:\n"
            "        return {'ok': False, 'status': 401, 'auth_resource': 'notes'}\n"
            "    return {'ok': True}\n"
        )
        tool = {**_tools()[0], "name": "both", "function": "both"}
        tool.pop("auth_resources")
        spec = {"code": code, "auth": _cfg(), "tools": [tool], "_config_path": "/tmp/prov.yaml"}
        handler = next(iter(server.build_tool_handlers(spec).values()))[1]
        prompt = await handler(context={}, caller_key="k")  # first use: sign in (inline)
        assert prompt["status"] == "authorization_required"
        assert (await handler(context={}, caller_key="k")) == {"ok": True}  # 401 → retry
        seen = handler.__wrapped__.__globals__["seen"]
        assert len(seen) == 2
        assert seen[0][0] == seen[1][0]  # api token unchanged
        assert seen[0][1] != seen[1][1]  # notes token refreshed

    def test_encrypt_with_secret_must_be_declared_on_every_tool(self):
        tools = _tools()
        tools[1]["secrets"] = {}
        errors = code_auth.validate_auth_config(_cfg(encrypt_with_secret="caller_key"), tools)
        assert any("encrypt_with_secret" in e and "'notes'" in e for e in errors)

    def test_auth_resources_must_be_known(self):
        tools = _tools()
        tools[0]["auth_resources"] = ["nope"]
        assert any("auth_resources" in e for e in code_auth.validate_auth_config(_cfg(), tools))

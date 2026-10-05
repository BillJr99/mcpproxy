"""UI routes for managed sign-in on code providers.

Covers the Authorize button path (``/api/rest-authorize``) for a code
provider, a per-provider (non-localhost) redirect completed through
``/api/oauth-manual-callback``, the device-code status / login / logout
endpoints, the pending-auth feed, auth info, validation, and that a provider's
``auth:`` block survives an edit-and-save round trip.
"""
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml
from fastapi.testclient import TestClient

import code_auth
import device_code_auth as dca
import rest_provider
from frontend.app import (
    _extract_secret_env_keys,
    _provider_auth_info,
    _provider_to_structured,
    _structured_to_yaml,
    _validate_provider,
    create_app,
)
from rest_provider import AuthCodeTokenStore, pending_rest_auth

CODE_SECRET = "one-time-auth-code-QQQQ9999"
CALLER_KEY = "caller-key-value-UI-0003"

AUTH_CODE = {
    "type": "authorization_code",
    "inject_as": "access_token",
    "authorize_url": "https://account.example.test/oauth2/authorize",
    "token_url": "https://api.example.test/oauth2/token",
    "client_id_env": "UI_CLIENT_ID",
    "client_secret_env": "UI_CLIENT_SECRET",
}

DEVICE = {
    "type": "device_code",
    "inject_as": "api_token",
    "device_authorization_url": "https://login.example.test/devicecode",
    "token_url": "https://login.example.test/token",
    "client_id": "public-client",
    "resources": {"api": {"scopes": "api offline_access"}},
    "encrypt_with_secret": "caller_key",
}


def _code_spec(auth: dict, *, secrets: dict | None = None) -> dict:
    tool = {
        "name": "whoami",
        "function": "whoami",
        "description": "The signed-in user.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    }
    if secrets:
        tool["secrets"] = secrets
    return {
        "auth": auth,
        "code": "async def whoami(context, **kw):\n    return {'ok': True}\n",
        "tools": [tool],
    }


DEVICE_SECRETS = {"env": {"caller_key": "UI_CALLER_KEY"}, "headers": {"caller_key": "X-UI-Key"}}


class FakeResponse:
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json = json_data or {}
        self.text = json.dumps(self._json)

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


@pytest.fixture()
def tools_dir(tmp_path: Path) -> Path:
    d = tmp_path / "tools"
    d.mkdir()
    return d


@pytest.fixture()
def client(tools_dir, tmp_path):
    return TestClient(create_app(config_dir=tools_dir, env_file=tmp_path / ".env"))


@pytest.fixture()
def auth_dir(tmp_path, monkeypatch):
    d = tmp_path / "rest-auth"
    monkeypatch.setattr(rest_provider, "REST_AUTH_DIR", d)
    monkeypatch.setattr(dca, "REST_AUTH_DIR", d)
    return d


@pytest.fixture()
def token_endpoint(monkeypatch):
    calls: list[dict] = []

    class Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, data=None, **kw):
            calls.append({"url": url, "data": dict(data or {})})
            return FakeResponse(json_data={"access_token": "ui-access-token-0001",
                                           "refresh_token": "ui-refresh-token-0001",
                                           "expires_in": 3600})

    monkeypatch.setattr(rest_provider.httpx, "AsyncClient", Client)
    return calls


@pytest.fixture()
def device_idp(monkeypatch):
    calls: list[dict] = []

    def post(url, data):
        calls.append({"url": url, "data": dict(data)})
        if url.endswith("/devicecode"):
            return 200, {"device_code": "device-code-secret-0001", "user_code": "ABCD-EFGH",
                         "verification_uri": "https://login.example.test/device",
                         "expires_in": 600, "interval": 5}
        return 200, {"access_token": "device-access-token-0001",
                     "refresh_token": "device-refresh-token-0001", "expires_in": 3600}

    monkeypatch.setattr(dca, "_http_post", post)
    monkeypatch.setattr(dca, "_sleep", lambda s: None)
    return calls


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setenv("UI_CLIENT_ID", "ui-client-id")
    monkeypatch.setenv("UI_CLIENT_SECRET", "ui-client-secret-value")
    pending_rest_auth.clear()
    AuthCodeTokenStore._pending_flows.clear()
    dca.reset_state()
    code_auth._providers.clear()
    yield
    pending_rest_auth.clear()
    AuthCodeTokenStore._pending_flows.clear()
    dca.reset_state()


def _write(tools_dir: Path, name: str, spec: dict) -> None:
    (tools_dir / f"{name}.yaml").write_text(yaml.safe_dump(spec, sort_keys=False))


# ---------------------------------------------------------------------------
# Authorize button + per-provider redirect
# ---------------------------------------------------------------------------

class TestAuthorizeCodeProvider:
    def test_authorize_button_path_for_a_code_provider(self, client, tools_dir, auth_dir):
        _write(tools_dir, "files", _code_spec(AUTH_CODE))
        body = client.post("/api/rest-authorize", json={"name": "files"}).json()
        assert body["ok"] is True
        assert body["auth_url"].startswith("https://account.example.test/oauth2/authorize?")
        assert body["redirect_uri"].endswith("/oauth/callback")
        assert "manual_callback_required" not in body
        assert pending_rest_auth["files"] == body["auth_url"]

    def test_code_provider_without_authorization_code_is_refused(self, client, tools_dir):
        _write(tools_dir, "plain", _code_spec({"type": "bearer", "inject_as": "t", "token_env": "X"}))
        assert client.post("/api/rest-authorize", json={"name": "plain"}).status_code == 400

    @pytest.mark.parametrize("named_target", [False, True])
    def test_non_localhost_redirect_completes_through_paste(
        self, client, tools_dir, auth_dir, token_endpoint, named_target
    ):
        _write(tools_dir, "files", _code_spec({**AUTH_CODE, "redirect_uri": "https://app.example.test/"}))
        body = client.post("/api/rest-authorize", json={"name": "files"}).json()
        assert body["redirect_uri"] == "https://app.example.test/"
        assert body["manual_callback_required"] is True
        state = parse_qs(urlsplit(body["auth_url"]).query)["state"][0]
        assert AuthCodeTokenStore._pending_flows[state]["redirect_uri"] == "https://app.example.test/"

        pasted = f"https://app.example.test/?code={CODE_SECRET}&state={state}"
        r = client.post("/api/oauth-manual-callback",
                        json={"callback": pasted, "target": "files" if named_target else ""})
        assert r.status_code == 200
        result = r.json()
        assert result["ok"] is True and result["mode"] == "in-process"
        assert CODE_SECRET not in r.text
        assert token_endpoint[0]["data"]["redirect_uri"] == "https://app.example.test/"
        assert token_endpoint[0]["data"]["code"] == CODE_SECRET
        saved = json.loads((auth_dir / "files.json").read_text())
        assert saved["access_token"] == "ui-access-token-0001"
        assert "files" not in pending_rest_auth

    def test_paste_for_the_wrong_provider_is_refused(self, client, tools_dir, auth_dir, token_endpoint):
        _write(tools_dir, "files", _code_spec({**AUTH_CODE, "redirect_uri": "https://app.example.test/"}))
        _write(tools_dir, "other", _code_spec(AUTH_CODE))
        url = client.post("/api/rest-authorize", json={"name": "files"}).json()["auth_url"]
        client.post("/api/rest-authorize", json={"name": "other"})
        state = parse_qs(urlsplit(url).query)["state"][0]
        r = client.post("/api/oauth-manual-callback",
                        json={"callback": f"https://app.example.test/?code={CODE_SECRET}&state={state}",
                              "target": "other"})
        assert r.status_code == 409
        assert CODE_SECRET not in r.text
        assert state in AuthCodeTokenStore._pending_flows  # not spent
        assert token_endpoint == []

    def test_failed_exchange_never_echoes_the_code(self, client, tools_dir, auth_dir, monkeypatch):
        _write(tools_dir, "files", _code_spec({**AUTH_CODE, "redirect_uri": "https://app.example.test/"}))
        url = client.post("/api/rest-authorize", json={"name": "files"}).json()["auth_url"]
        state = parse_qs(urlsplit(url).query)["state"][0]

        class Boom:
            def __init__(self, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def post(self, url, data=None, **kw):
                raise RuntimeError(f"POST {url}?code={data['code']} failed")

        monkeypatch.setattr(rest_provider.httpx, "AsyncClient", Boom)
        r = client.post("/api/oauth-manual-callback",
                        json={"callback": f"https://app.example.test/?code={CODE_SECRET}&state={state}"})
        assert r.json()["ok"] is False
        assert CODE_SECRET not in r.text


# ---------------------------------------------------------------------------
# Device-code endpoints
# ---------------------------------------------------------------------------

class TestDeviceEndpoints:
    def test_login_status_logout(self, client, tools_dir, auth_dir, device_idp, monkeypatch, capsys):
        monkeypatch.setattr(dca, "_spawn", lambda target: target())
        _write(tools_dir, "notes", _code_spec(DEVICE, secrets=DEVICE_SECRETS))

        r = client.post("/api/device-auth/login", json={"name": "notes", "key": CALLER_KEY})
        body = r.json()
        assert body["ok"] is True
        assert body["user_code"] == "ABCD-EFGH"
        assert body["verification_uri"] == "https://login.example.test/device"
        assert CALLER_KEY not in r.text and "device-code-secret" not in r.text

        raw = (auth_dir / "notes.device.json").read_bytes()
        assert b"device-access-token-0001" not in raw and CALLER_KEY.encode() not in raw

        status = client.post("/api/device-auth/status", json={"name": "notes", "key": CALLER_KEY}).json()
        assert status["status"]["signed_in"] is True
        assert status["status"]["resources"]["api"]["has_access_token"] is True
        assert "device-access-token-0001" not in json.dumps(status)

        wrong = client.post("/api/device-auth/status", json={"name": "notes", "key": "wrong-key-value"}).json()
        assert wrong["status"]["error_status"] == "key_mismatch"
        assert "wrong-key-value" not in json.dumps(wrong)

        out = client.post("/api/device-auth/logout", json={"name": "notes"}).json()
        assert out == {"ok": True, "removed_cache": True}
        assert not (auth_dir / "notes.device.json").exists()

        printed = capsys.readouterr()
        for secret in (CALLER_KEY, "device-access-token-0001", "device-refresh-token-0001",
                       "device-code-secret-0001"):
            assert secret not in printed.out and secret not in printed.err

    def test_key_falls_back_to_the_env_mapping(self, client, tools_dir, auth_dir, device_idp, monkeypatch):
        monkeypatch.setattr(dca, "_spawn", lambda target: target())
        monkeypatch.setenv("UI_CALLER_KEY", CALLER_KEY)
        _write(tools_dir, "notes", _code_spec(DEVICE, secrets=DEVICE_SECRETS))
        assert client.post("/api/device-auth/login", json={"name": "notes"}).json()["ok"] is True
        status = client.get("/api/device-auth/status", params={"name": "notes"}).json()
        assert status["status"]["signed_in"] is True

    def test_missing_key_is_a_clean_error(self, client, tools_dir, auth_dir, device_idp, monkeypatch):
        monkeypatch.delenv("UI_CALLER_KEY", raising=False)
        _write(tools_dir, "notes", _code_spec(DEVICE, secrets=DEVICE_SECRETS))
        body = client.post("/api/device-auth/login", json={"name": "notes"}).json()
        assert body == {"ok": False, "status": "no_credential", "error": body["error"]}

    def test_pending_feed_and_manual_callback_refusal(self, client, tools_dir, auth_dir, device_idp, monkeypatch):
        monkeypatch.setattr(dca, "_spawn", lambda target: None)  # user has not finished
        _write(tools_dir, "notes", _code_spec(DEVICE, secrets=DEVICE_SECRETS))
        client.post("/api/device-auth/login", json={"name": "notes", "key": CALLER_KEY})
        feed = client.get("/api/pending-auth").json()
        assert feed["device_pending"]["notes"]["user_code"] == "ABCD-EFGH"
        assert feed["pending"]["notes"] == "https://login.example.test/device"
        assert "device-code-secret" not in json.dumps(feed)
        r = client.post("/api/oauth-manual-callback",
                        json={"callback": "https://x.test/?code=c&state=s", "target": "notes"})
        assert r.status_code == 400

    def test_non_device_provider_is_refused(self, client, tools_dir):
        _write(tools_dir, "files", _code_spec(AUTH_CODE))
        assert client.post("/api/device-auth/logout", json={"name": "files"}).status_code == 400


# ---------------------------------------------------------------------------
# Structured round trip, validation, auth info
# ---------------------------------------------------------------------------

class TestStructured:
    def test_round_trip_keeps_auth_and_per_tool_keys(self):
        spec = _code_spec({**DEVICE, "inject_as": {"api": "api_token"}}, secrets=DEVICE_SECRETS)
        spec["tools"][0]["retry_on_401"] = False
        spec["tools"][0]["auth_resources"] = ["api"]
        again = yaml.safe_load(_structured_to_yaml(_provider_to_structured("notes", spec)))
        assert again["auth"] == spec["auth"]
        assert again["tools"][0]["retry_on_401"] is False
        assert again["tools"][0]["auth_resources"] == ["api"]

    def test_round_trip_unchanged_without_auth(self):
        spec = _code_spec(AUTH_CODE)
        del spec["auth"]
        out = _structured_to_yaml(_provider_to_structured("plain", spec))
        assert "auth" not in yaml.safe_load(out)
        assert "retry_on_401" not in out

    def test_validation_rejects_a_clash(self):
        spec = _code_spec(AUTH_CODE)
        spec["tools"][0]["input_schema"]["properties"]["access_token"] = {"type": "string"}
        result = _validate_provider(_provider_to_structured("files", spec))
        assert result["ok"] is False
        assert any("clashes with a parameter" in e for e in result["errors"])

    def test_validation_requires_the_encryption_secret_on_every_tool(self):
        result = _validate_provider(_provider_to_structured("notes", _code_spec(DEVICE)))
        assert any("encrypt_with_secret" in e for e in result["errors"])
        ok = _validate_provider(_provider_to_structured("notes", _code_spec(DEVICE, secrets=DEVICE_SECRETS)))
        assert ok["ok"] is True, ok["errors"]

    def test_validation_flags_auth_without_inject_as(self):
        result = _validate_provider(_provider_to_structured("x", _code_spec({"type": "bearer", "token_env": "T"})))
        assert any("inject_as" in e for e in result["errors"])

    def test_rest_redirect_uri_is_validated(self):
        provider = {
            "type": "rest", "name": "r", "code": "",
            "rest": {"base_url": "https://api.example.test",
                     "auth": {"type": "authorization_code", "authorize_url": "a", "token_url": "t",
                              "client_id_env": "C", "redirect_uri": "not-a-url"},
                     "endpoints": [{"name": "x", "method": "GET", "path": "/x"}]},
            "tools": [{"name": "x", "description": "x", "parameters": [], "secrets": []}],
        }
        assert any("redirect_uri" in e for e in _validate_provider(provider)["errors"])

    def test_auth_info_and_secret_keys(self):
        spec = _code_spec(AUTH_CODE)
        info = _provider_auth_info(spec)
        assert info["kind"] == "code" and info["type"] == "authorization_code" and info["renewable"]
        assert {"UI_CLIENT_ID", "UI_CLIENT_SECRET"} <= set(_extract_secret_env_keys(spec))

    def test_auth_refresh_for_a_code_provider(self, client, tools_dir, auth_dir, token_endpoint):
        auth_dir.mkdir(parents=True)
        (auth_dir / "files.json").write_text(json.dumps(
            {"access_token": "old", "refresh_token": "old-refresh", "expires_at": 0}))
        _write(tools_dir, "files", _code_spec(AUTH_CODE))
        body = client.post("/api/auth-refresh", json={"name": "files"}).json()
        assert body["ok"] is True and body["refreshed"] is True
        assert token_endpoint[0]["data"]["grant_type"] == "refresh_token"

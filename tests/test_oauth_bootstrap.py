"""Unit tests for oauth_bootstrap — provider-declared Google OAuth bootstrap.

HTTP is faked by patching ``oauth_bootstrap.httpx`` with a recording stub
(same pattern as tests/test_rest_provider.py), so no network is touched.
"""
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

import pytest

import oauth_bootstrap
import rest_provider
from oauth_bootstrap import (
    begin_authorization,
    complete_authorization,
    load_client_secret,
    token_status,
    warm_provider,
)
from rest_provider import AuthCodeTokenStore


class FakeResponse:
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json = json_data

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeAsyncClient:
    def __init__(self, recorder, **kwargs):
        self._recorder = recorder

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, data=None, **kwargs):
        self._recorder["calls"].append({"method": "POST", "url": url, "data": data})
        return self._recorder["responses"].pop(0)


@pytest.fixture()
def http_recorder(monkeypatch):
    recorder = {"calls": [], "responses": []}
    monkeypatch.setattr(
        oauth_bootstrap.httpx, "AsyncClient", lambda **kw: FakeAsyncClient(recorder, **kw)
    )
    return recorder


@pytest.fixture(autouse=True)
def _clear_flow_state():
    rest_provider.pending_rest_auth.clear()
    AuthCodeTokenStore._pending_flows.clear()
    yield
    rest_provider.pending_rest_auth.clear()
    AuthCodeTokenStore._pending_flows.clear()


@pytest.fixture()
def client_secret_file(tmp_path: Path) -> Path:
    path = tmp_path / "client_secret.json"
    path.write_text(json.dumps({
        "installed": {
            "client_id": "abc.apps.googleusercontent.com",
            "client_secret": "shhh",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }))
    return path


@pytest.fixture()
def oauth_cfg(client_secret_file: Path, tmp_path: Path) -> dict:
    return {
        "type": "google",
        "client_secret_file": str(client_secret_file),
        "token_file": str(tmp_path / "secrets" / "gmail_token.json"),
        "scopes": [
            "https://www.googleapis.com/auth/gmail.settings.basic",
            "https://www.googleapis.com/auth/gmail.labels",
        ],
    }


# ---------------------------------------------------------------------------
# load_client_secret
# ---------------------------------------------------------------------------

class TestLoadClientSecret:
    def test_installed_key(self, client_secret_file):
        out = load_client_secret(client_secret_file)
        assert out["client_id"] == "abc.apps.googleusercontent.com"
        assert out["client_secret"] == "shhh"
        assert out["token_uri"] == "https://oauth2.googleapis.com/token"

    def test_web_key_with_defaults(self, tmp_path):
        path = tmp_path / "cs.json"
        path.write_text(json.dumps({"web": {"client_id": "web-id", "client_secret": "s"}}))
        out = load_client_secret(path)
        assert out["client_id"] == "web-id"
        assert out["auth_uri"] == oauth_bootstrap.GOOGLE_AUTH_URI
        assert out["token_uri"] == oauth_bootstrap.GOOGLE_TOKEN_URI

    def test_missing_file(self, tmp_path):
        with pytest.raises(ValueError, match="not found"):
            load_client_secret(tmp_path / "nope.json")

    def test_garbage_json(self, tmp_path):
        path = tmp_path / "cs.json"
        path.write_text("{not json")
        with pytest.raises(ValueError, match="not valid JSON"):
            load_client_secret(path)

    def test_neither_key(self, tmp_path):
        path = tmp_path / "cs.json"
        path.write_text(json.dumps({"other": {}}))
        with pytest.raises(ValueError, match="installed"):
            load_client_secret(path)

    def test_missing_client_id(self, tmp_path):
        path = tmp_path / "cs.json"
        path.write_text(json.dumps({"installed": {"client_secret": "s"}}))
        with pytest.raises(ValueError, match="client_id"):
            load_client_secret(path)


# ---------------------------------------------------------------------------
# begin_authorization
# ---------------------------------------------------------------------------

class TestBeginGoogle:
    def test_url_params_and_flow_registration(self, oauth_cfg):
        url = begin_authorization("gmail-filters", oauth_cfg)
        parsed = urlparse(url)
        assert parsed.netloc == "accounts.google.com"
        q = parse_qs(parsed.query)
        assert q["access_type"] == ["offline"]
        assert q["prompt"] == ["consent"]
        assert q["code_challenge_method"] == ["S256"]
        assert q["client_id"] == ["abc.apps.googleusercontent.com"]
        assert q["scope"][0].split() == oauth_cfg["scopes"]
        assert q["redirect_uri"][0].endswith("/oauth/callback")

        state = q["state"][0]
        flow = AuthCodeTokenStore._pending_flows[state]
        assert flow["kind"] == "google"
        assert flow["provider"] == "gmail-filters"
        assert flow["token_file"] == oauth_cfg["token_file"]
        assert rest_provider.pending_rest_auth["gmail-filters"] == url

    def test_prompt_and_login_hint_overrides(self, oauth_cfg):
        oauth_cfg["prompt"] = "select_account"
        oauth_cfg["login_hint"] = "me@example.com"
        url = begin_authorization("p", oauth_cfg)
        q = parse_qs(urlparse(url).query)
        assert q["prompt"] == ["select_account"]
        assert q["login_hint"] == ["me@example.com"]

    def test_unsupported_type(self):
        with pytest.raises(ValueError, match="Unsupported oauth.type"):
            begin_authorization("p", {"type": "github"})

    def test_missing_client_secret_file_surfaces(self, oauth_cfg):
        oauth_cfg["client_secret_file"] = "/does/not/exist.json"
        with pytest.raises(ValueError, match="not found"):
            begin_authorization("p", oauth_cfg)


# ---------------------------------------------------------------------------
# complete_authorization (google) — token exchange + token file
# ---------------------------------------------------------------------------

def _begin_and_get_flow(oauth_cfg, provider="gmail-filters"):
    url = begin_authorization(provider, oauth_cfg)
    state = parse_qs(urlparse(url).query)["state"][0]
    return state, AuthCodeTokenStore._pending_flows[state]


class TestCompleteGoogle:
    def test_exchange_and_token_file(self, oauth_cfg, http_recorder):
        state, flow = _begin_and_get_flow(oauth_cfg)
        http_recorder["responses"].append(FakeResponse(json_data={
            "access_token": "at-123",
            "refresh_token": "rt-456",
            "expires_in": 3600,
            "scope": " ".join(oauth_cfg["scopes"]),
        }))
        access = asyncio.run(complete_authorization(flow, "the-code"))
        assert access == "at-123"

        call = http_recorder["calls"][0]
        assert call["url"] == "https://oauth2.googleapis.com/token"
        assert call["data"]["grant_type"] == "authorization_code"
        assert call["data"]["code"] == "the-code"
        assert call["data"]["client_id"] == "abc.apps.googleusercontent.com"
        assert call["data"]["client_secret"] == "shhh"
        assert call["data"]["code_verifier"] == flow["code_verifier"]
        assert call["data"]["redirect_uri"].endswith("/oauth/callback")

        record = json.loads(Path(oauth_cfg["token_file"]).read_text())
        assert record["token"] == "at-123"
        assert record["refresh_token"] == "rt-456"
        assert record["token_uri"] == "https://oauth2.googleapis.com/token"
        assert record["client_id"] == "abc.apps.googleusercontent.com"
        assert record["client_secret"] == "shhh"
        assert record["scopes"] == oauth_cfg["scopes"]
        assert record["universe_domain"] == "googleapis.com"
        assert record["expiry"].endswith("Z")
        # pending banner entry cleared
        assert "gmail-filters" not in rest_provider.pending_rest_auth

    def test_token_file_loads_with_google_auth(self, oauth_cfg, http_recorder):
        creds_mod = pytest.importorskip("google.oauth2.credentials")
        _, flow = _begin_and_get_flow(oauth_cfg)
        http_recorder["responses"].append(FakeResponse(json_data={
            "access_token": "at", "refresh_token": "rt", "expires_in": 3600,
        }))
        asyncio.run(complete_authorization(flow, "c"))
        creds = creds_mod.Credentials.from_authorized_user_file(oauth_cfg["token_file"])
        assert creds.refresh_token == "rt"
        assert creds.client_id == "abc.apps.googleusercontent.com"

    def test_no_refresh_token_raises_with_consent_hint(self, oauth_cfg, http_recorder):
        _, flow = _begin_and_get_flow(oauth_cfg)
        http_recorder["responses"].append(FakeResponse(json_data={"access_token": "at"}))
        with pytest.raises(RuntimeError, match="prompt=consent"):
            asyncio.run(complete_authorization(flow, "c"))

    def test_no_refresh_token_salvaged_from_prior_file(self, oauth_cfg, http_recorder):
        prior = Path(oauth_cfg["token_file"])
        prior.parent.mkdir(parents=True)
        prior.write_text(json.dumps({"refresh_token": "rt-old"}))
        _, flow = _begin_and_get_flow(oauth_cfg)
        http_recorder["responses"].append(FakeResponse(json_data={"access_token": "at-new"}))
        asyncio.run(complete_authorization(flow, "c"))
        record = json.loads(prior.read_text())
        assert record["token"] == "at-new"
        assert record["refresh_token"] == "rt-old"

    def test_unknown_kind(self):
        with pytest.raises(RuntimeError, match="Unknown authorization flow kind"):
            asyncio.run(complete_authorization({"kind": "mystery"}, "c"))


# ---------------------------------------------------------------------------
# Dispatch through AuthCodeTokenStore.complete_authorization (shared callback)
# ---------------------------------------------------------------------------

class TestCallbackDispatch:
    def test_google_flow_routed_to_oauth_bootstrap(self, oauth_cfg):
        state, _ = _begin_and_get_flow(oauth_cfg)
        with patch("oauth_bootstrap.complete_authorization",
                   new=AsyncMock(return_value="at")) as mock_complete:
            out = asyncio.run(AuthCodeTokenStore.complete_authorization(state, "code-1"))
        assert out == "at"
        mock_complete.assert_awaited_once()
        flow_arg, code_arg = mock_complete.await_args.args
        assert flow_arg["kind"] == "google"
        assert code_arg == "code-1"
        # the flow is consumed
        assert state not in AuthCodeTokenStore._pending_flows

    def test_rest_flow_still_uses_old_path(self, monkeypatch, tmp_path):
        # Regression: a flow without a "kind" follows the original REST exchange.
        monkeypatch.setenv("CID", "client-id")
        monkeypatch.setattr(rest_provider, "REST_AUTH_DIR", tmp_path)
        store = AuthCodeTokenStore("restp", {
            "client_id_env": "CID",
            "authorize_url": "https://auth.example/authorize",
            "token_url": "https://auth.example/token",
        })
        url = store.begin_authorization()
        state = parse_qs(urlparse(url).query)["state"][0]

        recorder = {"calls": [], "responses": [FakeResponse(json_data={
            "access_token": "rest-at", "refresh_token": "rest-rt", "expires_in": 60,
        })]}
        monkeypatch.setattr(
            rest_provider.httpx, "AsyncClient", lambda **kw: FakeAsyncClient(recorder, **kw)
        )
        out = asyncio.run(AuthCodeTokenStore.complete_authorization(state, "c"))
        assert out == "rest-at"
        assert recorder["calls"][0]["url"] == "https://auth.example/token"


# ---------------------------------------------------------------------------
# token_status / warm_provider
# ---------------------------------------------------------------------------

class TestTokenStatus:
    def test_missing_file(self, oauth_cfg):
        s = token_status(oauth_cfg)
        assert s == {"token_file": oauth_cfg["token_file"], "present": False,
                     "has_refresh_token": False, "expiry": None}

    def test_file_without_refresh_token(self, oauth_cfg):
        p = Path(oauth_cfg["token_file"])
        p.parent.mkdir(parents=True)
        p.write_text(json.dumps({"token": "at"}))
        s = token_status(oauth_cfg)
        assert s["present"] is True and s["has_refresh_token"] is False

    def test_good_file(self, oauth_cfg):
        p = Path(oauth_cfg["token_file"])
        p.parent.mkdir(parents=True)
        p.write_text(json.dumps({"refresh_token": "rt", "expiry": "2030-01-01T00:00:00Z"}))
        s = token_status(oauth_cfg)
        assert s["has_refresh_token"] is True
        assert s["expiry"] == "2030-01-01T00:00:00Z"


class TestWarmProvider:
    def test_publishes_url_when_no_token(self, oauth_cfg):
        asyncio.run(warm_provider("gmail-filters", oauth_cfg))
        assert "gmail-filters" in rest_provider.pending_rest_auth

    def test_noop_when_refresh_token_present(self, oauth_cfg):
        p = Path(oauth_cfg["token_file"])
        p.parent.mkdir(parents=True)
        p.write_text(json.dumps({"refresh_token": "rt"}))
        asyncio.run(warm_provider("gmail-filters", oauth_cfg))
        assert "gmail-filters" not in rest_provider.pending_rest_auth

    def test_never_raises_on_bad_config(self):
        asyncio.run(warm_provider("p", {"type": "google",
                                        "client_secret_file": "/missing.json",
                                        "token_file": "/also/missing.json"}))
        # bad config logs but must not raise or publish
        assert "p" not in rest_provider.pending_rest_auth

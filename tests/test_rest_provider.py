"""Unit tests for rest_provider — handlers, OAuth managers, OpenAPI introspection.

HTTP is faked by patching ``rest_provider.httpx`` with a recording stub, so no
network is touched.
"""
import asyncio
import json
from pathlib import Path

import pytest

import rest_provider
from rest_provider import (
    AuthCodeTokenStore,
    NeedsAuthorization,
    OAuthTokenManager,
    _make_rest_handler,
    _split_kwargs,
    introspect_openapi,
    resolve_rest_auth,
)


# ---------------------------------------------------------------------------
# httpx fakes
# ---------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=None):
        self.status_code = status_code
        self._json = json_data
        self.text = text if text is not None else json.dumps(json_data or {})

    def json(self):
        if self._json is None:
            raise json.JSONDecodeError("no json", "", 0)
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeAsyncClient:
    """Records calls and returns queued responses.  Shared via a factory."""

    def __init__(self, recorder, **kwargs):
        self._recorder = recorder

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def request(self, method, url, params=None, json=None, headers=None):
        self._recorder["calls"].append(
            {"method": method, "url": url, "params": params, "json": json, "headers": headers}
        )
        return self._recorder["responses"].pop(0)

    async def post(self, url, data=None, **kwargs):
        self._recorder["calls"].append({"method": "POST", "url": url, "data": data})
        return self._recorder["responses"].pop(0)


@pytest.fixture()
def http_recorder(monkeypatch):
    """Patch rest_provider.httpx.AsyncClient with a recording fake."""
    recorder = {"calls": [], "responses": []}

    def factory(**kwargs):
        return FakeAsyncClient(recorder, **kwargs)

    monkeypatch.setattr(rest_provider.httpx, "AsyncClient", factory)
    return recorder


@pytest.fixture(autouse=True)
def _clear_token_state():
    rest_provider._token_managers.clear()
    rest_provider.pending_rest_auth.clear()
    AuthCodeTokenStore._pending_flows.clear()
    yield
    rest_provider._token_managers.clear()
    rest_provider.pending_rest_auth.clear()
    AuthCodeTokenStore._pending_flows.clear()


# ---------------------------------------------------------------------------
# _split_kwargs
# ---------------------------------------------------------------------------

class TestSplitKwargs:
    def test_classifies_by_metadata(self):
        ep = {
            "method": "POST",
            "path_params": ["id"],
            "query_params": ["q"],
            "body_params": ["title"],
        }
        path, query, body = _split_kwargs(ep, {"id": "1", "q": "x", "title": "t"})
        assert path == {"id": "1"}
        assert query == {"q": "x"}
        assert body == {"title": "t"}

    def test_drops_none_values(self):
        ep = {"method": "GET", "query_params": ["q"]}
        _, query, _ = _split_kwargs(ep, {"q": None})
        assert query == {}

    def test_unclassified_get_goes_to_query(self):
        ep = {"method": "GET"}
        _, query, body = _split_kwargs(ep, {"extra": "v"})
        assert query == {"extra": "v"} and body == {}

    def test_unclassified_post_goes_to_body(self):
        ep = {"method": "POST"}
        _, query, body = _split_kwargs(ep, {"extra": "v"})
        assert body == {"extra": "v"} and query == {}


# ---------------------------------------------------------------------------
# OAuthTokenManager (client_credentials)
# ---------------------------------------------------------------------------

CC_AUTH = {
    "type": "client_credentials",
    "token_url": "https://auth/token",
    "client_id_env": "CC_ID",
    "client_secret_env": "CC_SECRET",
    "scopes": ["read"],
}


class TestOAuthTokenManager:
    def _mgr(self):
        return OAuthTokenManager("https://auth/token", "CC_ID", "CC_SECRET", ["read"])

    def test_fetches_token_on_first_call(self, http_recorder, monkeypatch):
        monkeypatch.setenv("CC_ID", "id")
        monkeypatch.setenv("CC_SECRET", "secret")
        http_recorder["responses"].append(FakeResponse(json_data={"access_token": "abc", "expires_in": 3600}))
        token = asyncio.run(self._mgr().get_token())
        assert token == "abc"
        assert http_recorder["calls"][0]["data"]["grant_type"] == "client_credentials"
        assert http_recorder["calls"][0]["data"]["scope"] == "read"

    def test_caches_token_until_expiry(self, http_recorder, monkeypatch):
        monkeypatch.setenv("CC_ID", "id")
        monkeypatch.setenv("CC_SECRET", "secret")
        http_recorder["responses"].append(FakeResponse(json_data={"access_token": "abc", "expires_in": 3600}))
        mgr = self._mgr()

        async def go():
            t1 = await mgr.get_token()
            t2 = await mgr.get_token()
            return t1, t2

        t1, t2 = asyncio.run(go())
        assert t1 == t2 == "abc"
        assert len(http_recorder["calls"]) == 1  # only one fetch

    def test_refreshes_after_expiry(self, http_recorder, monkeypatch):
        monkeypatch.setenv("CC_ID", "id")
        monkeypatch.setenv("CC_SECRET", "secret")
        http_recorder["responses"].append(FakeResponse(json_data={"access_token": "a", "expires_in": 0}))
        http_recorder["responses"].append(FakeResponse(json_data={"access_token": "b", "expires_in": 3600}))
        mgr = self._mgr()

        async def go():
            return await mgr.get_token(), await mgr.get_token()

        t1, t2 = asyncio.run(go())
        assert t1 == "a" and t2 == "b"
        assert len(http_recorder["calls"]) == 2

    def test_force_refresh_bypasses_cache(self, http_recorder, monkeypatch):
        monkeypatch.setenv("CC_ID", "id")
        monkeypatch.setenv("CC_SECRET", "secret")
        http_recorder["responses"].append(FakeResponse(json_data={"access_token": "a", "expires_in": 3600}))
        http_recorder["responses"].append(FakeResponse(json_data={"access_token": "b", "expires_in": 3600}))
        mgr = self._mgr()

        async def go():
            return await mgr.get_token(), await mgr.get_token(force_refresh=True)

        t1, t2 = asyncio.run(go())
        assert t1 == "a" and t2 == "b"

    def test_concurrent_calls_fetch_once(self, http_recorder, monkeypatch):
        monkeypatch.setenv("CC_ID", "id")
        monkeypatch.setenv("CC_SECRET", "secret")
        http_recorder["responses"].append(FakeResponse(json_data={"access_token": "abc", "expires_in": 3600}))
        mgr = self._mgr()

        async def go():
            return await asyncio.gather(mgr.get_token(), mgr.get_token(), mgr.get_token())

        tokens = asyncio.run(go())
        assert tokens == ["abc", "abc", "abc"]
        assert len(http_recorder["calls"]) == 1

    def test_missing_secret_raises(self, http_recorder, monkeypatch):
        monkeypatch.delenv("CC_ID", raising=False)
        with pytest.raises(RuntimeError, match="Missing required secret"):
            asyncio.run(self._mgr().get_token())

    def test_get_token_manager_shares_instance(self):
        a = rest_provider.get_token_manager(CC_AUTH)
        b = rest_provider.get_token_manager(CC_AUTH)
        assert a is b


# ---------------------------------------------------------------------------
# AuthCodeTokenStore (authorization_code + PKCE)
# ---------------------------------------------------------------------------

AC_AUTH = {
    "type": "authorization_code",
    "authorize_url": "https://auth/authorize",
    "token_url": "https://auth/token",
    "client_id_env": "AC_ID",
    "client_secret_env": "AC_SECRET",
    "scopes": ["read", "write"],
}


@pytest.fixture()
def rest_auth_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(rest_provider, "REST_AUTH_DIR", tmp_path / "rest-auth")
    return tmp_path / "rest-auth"


class TestAuthCodeTokenStore:
    def test_begin_authorization_builds_pkce_url_and_publishes(self, monkeypatch, rest_auth_dir):
        monkeypatch.setenv("AC_ID", "id")
        store = AuthCodeTokenStore("prov", AC_AUTH)
        url = store.begin_authorization()
        assert url.startswith("https://auth/authorize?")
        assert "code_challenge=" in url and "code_challenge_method=S256" in url
        assert "response_type=code" in url
        assert rest_provider.pending_rest_auth["prov"] == url
        assert len(AuthCodeTokenStore._pending_flows) == 1

    def test_complete_authorization_persists_tokens(self, http_recorder, monkeypatch, rest_auth_dir):
        monkeypatch.setenv("AC_ID", "id")
        monkeypatch.setenv("AC_SECRET", "secret")
        store = AuthCodeTokenStore("prov", AC_AUTH)
        store.begin_authorization()
        state = next(iter(AuthCodeTokenStore._pending_flows))
        http_recorder["responses"].append(
            FakeResponse(json_data={"access_token": "tok", "refresh_token": "ref", "expires_in": 3600})
        )
        access = asyncio.run(AuthCodeTokenStore.complete_authorization(state, "thecode"))
        assert access == "tok"
        # token persisted to disk and pending cleared
        assert (rest_auth_dir / "prov.json").exists()
        assert "prov" not in rest_provider.pending_rest_auth
        data = json.loads((rest_auth_dir / "prov.json").read_text())
        assert data["refresh_token"] == "ref"
        # the exchange POSTed the PKCE verifier + auth code
        exch = http_recorder["calls"][-1]
        assert exch["data"]["grant_type"] == "authorization_code"
        assert exch["data"]["code"] == "thecode"
        assert "code_verifier" in exch["data"]

    def test_complete_authorization_unknown_state_raises(self, rest_auth_dir):
        with pytest.raises(RuntimeError, match="Unknown or expired"):
            asyncio.run(AuthCodeTokenStore.complete_authorization("nope", "code"))

    def test_stale_pending_flow_is_pruned(self, monkeypatch, rest_auth_dir):
        monkeypatch.setenv("AC_ID", "id")
        monkeypatch.setattr(rest_provider, "_FLOW_TTL", 100)
        store = AuthCodeTokenStore("prov", AC_AUTH)
        store.begin_authorization()
        state = next(iter(AuthCodeTokenStore._pending_flows))
        # Age the flow past the TTL; the next begin prunes it.
        AuthCodeTokenStore._pending_flows[state]["created"] -= 200
        AuthCodeTokenStore("prov2", AC_AUTH).begin_authorization()
        assert state not in AuthCodeTokenStore._pending_flows

    def test_fresh_pending_flow_survives_prune(self, monkeypatch, rest_auth_dir):
        monkeypatch.setenv("AC_ID", "id")
        store = AuthCodeTokenStore("prov", AC_AUTH)
        store.begin_authorization()
        first = next(iter(AuthCodeTokenStore._pending_flows))
        AuthCodeTokenStore("prov2", AC_AUTH).begin_authorization()
        assert first in AuthCodeTokenStore._pending_flows  # not stale → kept

    def test_get_token_returns_cached(self, monkeypatch, rest_auth_dir):
        monkeypatch.setenv("AC_ID", "id")
        rest_auth_dir.mkdir(parents=True)
        (rest_auth_dir / "prov.json").write_text(
            json.dumps({"access_token": "cached", "refresh_token": "r", "expires_at": 9_999_999_999})
        )
        store = AuthCodeTokenStore("prov", AC_AUTH)
        assert asyncio.run(store.get_token()) == "cached"

    def test_get_token_refreshes_when_expired(self, http_recorder, monkeypatch, rest_auth_dir):
        monkeypatch.setenv("AC_ID", "id")
        monkeypatch.setenv("AC_SECRET", "secret")
        rest_auth_dir.mkdir(parents=True)
        (rest_auth_dir / "prov.json").write_text(
            json.dumps({"access_token": "old", "refresh_token": "r", "expires_at": 0})
        )
        http_recorder["responses"].append(
            FakeResponse(json_data={"access_token": "new", "refresh_token": "r2", "expires_in": 3600})
        )
        store = AuthCodeTokenStore("prov", AC_AUTH)
        assert asyncio.run(store.get_token()) == "new"
        assert http_recorder["calls"][-1]["data"]["grant_type"] == "refresh_token"

    def test_get_token_no_cache_raises_needs_authorization(self, monkeypatch, rest_auth_dir):
        monkeypatch.setenv("AC_ID", "id")
        store = AuthCodeTokenStore("prov", AC_AUTH)
        with pytest.raises(NeedsAuthorization):
            asyncio.run(store.get_token())
        assert "prov" in rest_provider.pending_rest_auth


# ---------------------------------------------------------------------------
# resolve_rest_auth
# ---------------------------------------------------------------------------

class TestResolveRestAuth:
    def _apply(self, auth):
        resolver = resolve_rest_auth("prov", {"auth": auth})
        headers: dict = {}
        asyncio.run(resolver.apply(headers))
        return headers, resolver

    def test_none_adds_no_header(self):
        headers, resolver = self._apply({"type": "none"})
        assert headers == {}
        assert resolver.supports_retry is False

    def test_bearer_sets_authorization_from_env(self, monkeypatch):
        monkeypatch.setenv("MY_TOKEN", "xyz")
        headers, _ = self._apply({"type": "bearer", "token_env": "MY_TOKEN"})
        assert headers["Authorization"] == "Bearer xyz"

    def test_api_key_sets_custom_header_from_env(self, monkeypatch):
        monkeypatch.setenv("MY_KEY", "k1")
        headers, _ = self._apply({"type": "api_key", "header": "X-Api-Key", "value_env": "MY_KEY"})
        assert headers["X-Api-Key"] == "k1"

    def test_client_credentials_sets_bearer(self, http_recorder, monkeypatch):
        monkeypatch.setenv("CC_ID", "id")
        monkeypatch.setenv("CC_SECRET", "secret")
        http_recorder["responses"].append(FakeResponse(json_data={"access_token": "cc", "expires_in": 3600}))
        headers, resolver = self._apply(CC_AUTH)
        assert headers["Authorization"] == "Bearer cc"
        assert resolver.supports_retry is True

    def test_api_key_in_query_uses_apply_query_not_header(self, monkeypatch):
        monkeypatch.setenv("MY_KEY", "qk")
        resolver = resolve_rest_auth("prov", {"auth": {
            "type": "api_key", "in": "query", "name": "apikey", "value_env": "MY_KEY"}})
        headers: dict = {}
        asyncio.run(resolver.apply(headers))
        assert headers == {}  # nothing in headers
        params: dict = {}
        resolver.apply_query(params)
        assert params == {"apikey": "qk"}

    def test_apply_query_noop_for_header_api_key(self, monkeypatch):
        monkeypatch.setenv("MY_KEY", "k")
        resolver = resolve_rest_auth("prov", {"auth": {
            "type": "api_key", "header": "X-Api-Key", "value_env": "MY_KEY"}})
        params: dict = {}
        resolver.apply_query(params)
        assert params == {}


# ---------------------------------------------------------------------------
# _make_rest_handler
# ---------------------------------------------------------------------------

REST_CONFIG = {
    "base_url": "https://api.example.com/v1",
    "headers": {"Accept": "application/json"},
    "auth": {"type": "none"},
}


class TestMakeRestHandler:
    def _call(self, endpoint, rest_config, kwargs):
        handler = _make_rest_handler(endpoint, rest_config, "prov")
        return asyncio.run(handler(context={}, **kwargs))

    def test_builds_url_with_path_params(self, http_recorder):
        http_recorder["responses"].append(FakeResponse(json_data={"id": "7"}))
        ep = {"name": "get_user", "method": "GET", "path": "/users/{user_id}",
              "path_params": ["user_id"], "query_params": [], "body_params": []}
        result = self._call(ep, REST_CONFIG, {"user_id": "7"})
        assert result == {"id": "7"}
        assert http_recorder["calls"][0]["url"] == "https://api.example.com/v1/users/7"

    def test_sends_query_and_body_separately(self, http_recorder):
        http_recorder["responses"].append(FakeResponse(json_data={"ok": True}))
        ep = {"name": "create", "method": "POST", "path": "/items",
              "path_params": [], "query_params": ["dry"], "body_params": ["title"]}
        self._call(ep, REST_CONFIG, {"dry": "1", "title": "hi"})
        call = http_recorder["calls"][0]
        assert call["params"] == {"dry": "1"}
        assert call["json"] == {"title": "hi"}

    def test_merges_default_headers(self, http_recorder, monkeypatch):
        monkeypatch.setenv("T", "tk")
        http_recorder["responses"].append(FakeResponse(json_data={}))
        cfg = {**REST_CONFIG, "auth": {"type": "bearer", "token_env": "T"}}
        ep = {"name": "g", "method": "GET", "path": "/x", "path_params": [], "query_params": [], "body_params": []}
        self._call(ep, cfg, {})
        headers = http_recorder["calls"][0]["headers"]
        assert headers["Accept"] == "application/json"
        assert headers["Authorization"] == "Bearer tk"

    def test_returns_parsed_json(self, http_recorder):
        http_recorder["responses"].append(FakeResponse(json_data={"v": 1}))
        ep = {"name": "g", "method": "GET", "path": "/x", "path_params": [], "query_params": [], "body_params": []}
        assert self._call(ep, REST_CONFIG, {}) == {"v": 1}

    def test_non_json_returns_text(self, http_recorder):
        http_recorder["responses"].append(FakeResponse(status_code=200, json_data=None, text="hello"))
        ep = {"name": "g", "method": "GET", "path": "/x", "path_params": [], "query_params": [], "body_params": []}
        result = self._call(ep, REST_CONFIG, {})
        assert result == {"ok": True, "status": 200, "text": "hello"}

    def test_http_error_returns_error_dict(self, http_recorder):
        http_recorder["responses"].append(FakeResponse(status_code=404, json_data=None, text="missing"))
        ep = {"name": "g", "method": "GET", "path": "/x", "path_params": [], "query_params": [], "body_params": []}
        result = self._call(ep, REST_CONFIG, {})
        assert result["ok"] is False and result["status"] == 404 and result["tool"] == "g"

    def test_401_triggers_refresh_and_retry_once(self, http_recorder, monkeypatch):
        monkeypatch.setenv("CC_ID", "id")
        monkeypatch.setenv("CC_SECRET", "secret")
        # token fetch, then a 401, then token refresh, then a 200
        http_recorder["responses"].append(FakeResponse(json_data={"access_token": "t1", "expires_in": 3600}))
        http_recorder["responses"].append(FakeResponse(status_code=401, json_data=None, text="unauth"))
        http_recorder["responses"].append(FakeResponse(json_data={"access_token": "t2", "expires_in": 3600}))
        http_recorder["responses"].append(FakeResponse(json_data={"ok": True}))
        cfg = {**REST_CONFIG, "auth": CC_AUTH}
        ep = {"name": "g", "method": "GET", "path": "/x", "path_params": [], "query_params": [], "body_params": []}
        result = self._call(ep, cfg, {})
        assert result == {"ok": True}
        # second request used the refreshed token
        request_calls = [c for c in http_recorder["calls"] if c.get("url", "").endswith("/x")]
        assert request_calls[-1]["headers"]["Authorization"] == "Bearer t2"

    def test_needs_authorization_surfaced_in_result(self, http_recorder, monkeypatch, rest_auth_dir):
        monkeypatch.setenv("AC_ID", "id")
        cfg = {**REST_CONFIG, "auth": AC_AUTH}
        ep = {"name": "g", "method": "GET", "path": "/x", "path_params": [], "query_params": [], "body_params": []}
        result = self._call(ep, cfg, {})
        assert result["ok"] is False and "auth_url" in result

    def test_api_key_query_added_to_request(self, http_recorder, monkeypatch):
        monkeypatch.setenv("QK", "secretkey")
        http_recorder["responses"].append(FakeResponse(json_data={"ok": True}))
        cfg = {**REST_CONFIG, "auth": {"type": "api_key", "in": "query", "name": "key", "value_env": "QK"}}
        ep = {"name": "g", "method": "GET", "path": "/x", "path_params": [], "query_params": ["q"], "body_params": []}
        self._call(ep, cfg, {"q": "term"})
        assert http_recorder["calls"][0]["params"] == {"q": "term", "key": "secretkey"}

    def test_large_response_is_truncated(self, http_recorder, monkeypatch):
        monkeypatch.setattr(rest_provider, "MAX_RESPONSE_BYTES", 50)
        big = "x" * 500
        http_recorder["responses"].append(FakeResponse(status_code=200, json_data=None, text=big))
        ep = {"name": "g", "method": "GET", "path": "/x", "path_params": [], "query_params": [], "body_params": []}
        result = self._call(ep, REST_CONFIG, {})
        assert result["truncated"] is True
        assert result["total_bytes"] == 500
        assert len(result["preview"]) == 50

    def test_small_response_not_truncated(self, http_recorder, monkeypatch):
        monkeypatch.setattr(rest_provider, "MAX_RESPONSE_BYTES", 100000)
        http_recorder["responses"].append(FakeResponse(json_data={"v": 1}))
        ep = {"name": "g", "method": "GET", "path": "/x", "path_params": [], "query_params": [], "body_params": []}
        assert self._call(ep, REST_CONFIG, {}) == {"v": 1}

    def test_truncation_disabled_with_zero(self, http_recorder, monkeypatch):
        monkeypatch.setattr(rest_provider, "MAX_RESPONSE_BYTES", 0)
        http_recorder["responses"].append(FakeResponse(json_data={"data": "y" * 500}))
        ep = {"name": "g", "method": "GET", "path": "/x", "path_params": [], "query_params": [], "body_params": []}
        result = self._call(ep, REST_CONFIG, {})
        assert result == {"data": "y" * 500}


# ---------------------------------------------------------------------------
# introspect_openapi
# ---------------------------------------------------------------------------

OPENAPI_DOC = {
    "openapi": "3.0.0",
    "paths": {
        "/users/{user_id}": {
            "get": {
                "operationId": "get_user",
                "summary": "Fetch a user",
                "parameters": [
                    {"name": "user_id", "in": "path", "required": True, "schema": {"type": "string"}},
                    {"name": "include", "in": "query", "schema": {"type": "string"}},
                ],
            }
        },
        "/items": {
            "post": {
                "operationId": "create_item",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["title"],
                                "properties": {
                                    "title": {"type": "string"},
                                    "count": {"type": "integer"},
                                },
                            }
                        }
                    },
                },
            }
        },
    },
}


class TestIntrospectOpenAPI:
    def _introspect(self, tmp_path):
        path = tmp_path / "openapi.json"
        path.write_text(json.dumps(OPENAPI_DOC))
        return introspect_openapi(str(path))

    def test_parses_paths_into_endpoints(self, tmp_path):
        endpoints, tools = self._introspect(tmp_path)
        names = {e["name"] for e in endpoints}
        assert names == {"get_user", "create_item"}
        assert len(tools) == 2

    def test_operationid_becomes_tool_name(self, tmp_path):
        _, tools = self._introspect(tmp_path)
        assert {t["name"] for t in tools} == {"get_user", "create_item"}

    def test_param_classification(self, tmp_path):
        endpoints, _ = self._introspect(tmp_path)
        get_user = next(e for e in endpoints if e["name"] == "get_user")
        assert get_user["path_params"] == ["user_id"]
        assert get_user["query_params"] == ["include"]
        create = next(e for e in endpoints if e["name"] == "create_item")
        assert set(create["body_params"]) == {"title", "count"}
        assert create["method"] == "POST"

    def test_builds_input_schema_with_required(self, tmp_path):
        _, tools = self._introspect(tmp_path)
        get_user = next(t for t in tools if t["name"] == "get_user")
        assert "user_id" in get_user["input_schema"]["properties"]
        assert get_user["input_schema"]["required"] == ["user_id"]
        create = next(t for t in tools if t["name"] == "create_item")
        assert "title" in create["input_schema"]["required"]

    def test_derives_name_when_no_operation_id(self, tmp_path):
        doc = {"openapi": "3.0.0", "paths": {"/a/b": {"get": {}}}}
        path = tmp_path / "o.json"
        path.write_text(json.dumps(doc))
        endpoints, _ = introspect_openapi(str(path))
        assert endpoints[0]["name"] == "get_a_b"

    def test_resolves_local_ref_for_param(self, tmp_path):
        doc = {
            "openapi": "3.0.0",
            "components": {"parameters": {"Id": {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}}},
            "paths": {"/x/{id}": {"get": {"operationId": "getx", "parameters": [{"$ref": "#/components/parameters/Id"}]}}},
        }
        path = tmp_path / "o.json"
        path.write_text(json.dumps(doc))
        endpoints, _ = introspect_openapi(str(path))
        assert endpoints[0]["path_params"] == ["id"]

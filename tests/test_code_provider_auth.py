"""Tests for managed sign-in on code providers (``code_auth``).

Covers credential injection for each auth type, the 401 refresh contract
(including the per-tool opt-out), the ``authorization_required`` result and
banner, per-provider redirect URIs, ``inject_as`` clash rejection, secret
scrubbing, startup warm-up (``warm_on_start``), and that a provider without a
managed ``auth:`` block is left completely untouched.

HTTP is faked by patching ``rest_provider.httpx.AsyncClient``; nothing touches
the network.
"""
import json
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

import code_auth
import rest_provider
import server
from rest_provider import AuthCodeTokenStore, pending_rest_auth

ACCESS_1 = "access-token-one-AAAA1111"
ACCESS_2 = "access-token-two-BBBB2222"
REFRESH_1 = "refresh-token-one-CCCC3333"
REFRESH_2 = "refresh-token-two-DDDD4444"


# ---------------------------------------------------------------------------
# Fakes and fixtures
# ---------------------------------------------------------------------------

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


class FakeAsyncClient:
    def __init__(self, recorder, **kwargs):
        self._recorder = recorder

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, data=None, **kwargs):
        self._recorder["calls"].append({"url": url, "data": dict(data or {})})
        return self._recorder["responses"].pop(0)


@pytest.fixture()
def http(monkeypatch):
    recorder = {"calls": [], "responses": []}
    monkeypatch.setattr(
        rest_provider.httpx, "AsyncClient", lambda **kw: FakeAsyncClient(recorder, **kw)
    )
    return recorder


@pytest.fixture()
def auth_dir(tmp_path, monkeypatch):
    d = tmp_path / "rest-auth"
    monkeypatch.setattr(rest_provider, "REST_AUTH_DIR", d)
    return d


@pytest.fixture(autouse=True)
def _clear_state():
    def clear():
        rest_provider._token_managers.clear()
        pending_rest_auth.clear()
        AuthCodeTokenStore._pending_flows.clear()
        code_auth._providers.clear()
    clear()
    yield
    clear()


def _spec(code: str, auth: dict | None, tools: list[dict], name: str = "demo") -> dict:
    spec = {"code": code, "tools": tools, "_config_path": f"/tmp/{name}.yaml"}
    if auth is not None:
        spec["auth"] = auth
    return spec


def _tool(name: str, **extra) -> dict:
    base = {
        "name": name,
        "function": name,
        "description": name,
        "input_schema": {"type": "object", "properties": {"item": {"type": "string"}}},
    }
    base.update(extra)
    return base


def _handlers(spec: dict) -> dict:
    return {k.split("__", 1)[1]: v[1] for k, v in server.build_tool_handlers(spec).items()}


AUTH_CODE = {
    "type": "authorization_code",
    "inject_as": "access_token",
    "authorize_url": "https://auth.example.test/authorize",
    "token_url": "https://auth.example.test/token",
    "client_id_env": "DEMO_CLIENT_ID",
    "client_secret_env": "DEMO_CLIENT_SECRET",
}

RECORDING_CODE = (
    "calls = []\n"
    "async def fetch(context, access_token, item: str = ''):\n"
    "    calls.append(access_token)\n"
    "    if access_token == 'access-token-one-AAAA1111':\n"
    "        return {'ok': False, 'status': 401, 'error': 'expired'}\n"
    "    return {'ok': True, 'item': item, 'n': len(calls)}\n"
)


def _save_token(auth_dir: Path, provider: str, access: str, refresh: str, expires_in: float = 3600):
    auth_dir.mkdir(parents=True, exist_ok=True)
    (auth_dir / f"{provider}.json").write_text(json.dumps({
        "access_token": access, "refresh_token": refresh, "expires_at": time.time() + expires_in,
    }))


@pytest.fixture()
def client_env(monkeypatch):
    monkeypatch.setenv("DEMO_CLIENT_ID", "client-id")
    monkeypatch.setenv("DEMO_CLIENT_SECRET", "client-secret-value")


# ---------------------------------------------------------------------------
# Opt-in and backward compatibility
# ---------------------------------------------------------------------------

class TestOptIn:
    def test_provider_without_auth_is_not_wrapped(self):
        code = "async def fetch(context, item: str = ''):\n    return {'ok': True}\n"
        spec = _spec(code, None, [_tool("fetch")])
        handler = _handlers(spec)["fetch"]
        assert getattr(handler, "__wrapped__", None) is None
        assert handler.__code__.co_filename == "/tmp/demo.yaml"

    def test_auth_without_inject_as_is_inert(self):
        code = "async def fetch(context, item: str = ''):\n    return {'ok': True}\n"
        spec = _spec(code, {"type": "bearer", "token_env": "X"}, [_tool("fetch")])
        assert code_auth.get_code_auth(spec) is None
        assert getattr(_handlers(spec)["fetch"], "__wrapped__", None) is None

    def test_rest_providers_never_use_code_auth(self):
        spec = {"rest": {"auth": {"type": "bearer"}}, "auth": {"type": "bearer", "inject_as": "t"}}
        assert code_auth.get_code_auth(spec) is None


# ---------------------------------------------------------------------------
# Injection
# ---------------------------------------------------------------------------

class TestInjection:
    @pytest.mark.asyncio
    async def test_bearer_token_is_injected(self, monkeypatch):
        monkeypatch.setenv("DEMO_BEARER", "bearer-value-1234")
        code = "async def fetch(context, token, item: str = ''):\n    return {'ok': True, 'token': token}\n"
        spec = _spec(code, {"type": "bearer", "inject_as": "token", "token_env": "DEMO_BEARER"}, [_tool("fetch")])
        result = await _handlers(spec)["fetch"](context={}, item="x")
        assert result == {"ok": True, "token": "bearer-value-1234"}

    @pytest.mark.asyncio
    async def test_api_key_is_injected(self, monkeypatch):
        monkeypatch.setenv("DEMO_KEY", "api-key-value-9876")
        code = "async def fetch(context, key):\n    return {'ok': True, 'key': key}\n"
        spec = _spec(code, {"type": "api_key", "inject_as": "key", "value_env": "DEMO_KEY"}, [_tool("fetch")])
        assert (await _handlers(spec)["fetch"](context={}))["key"] == "api-key-value-9876"

    @pytest.mark.asyncio
    async def test_client_credentials_refreshes_after_401_result(self, monkeypatch, http):
        monkeypatch.setenv("DEMO_CLIENT_ID", "client-id")
        monkeypatch.setenv("DEMO_CLIENT_SECRET", "client-secret-value")
        auth = {
            "type": "client_credentials", "inject_as": "access_token",
            "token_url": "https://auth.example.test/token",
            "client_id_env": "DEMO_CLIENT_ID", "client_secret_env": "DEMO_CLIENT_SECRET",
        }
        http["responses"] = [
            FakeResponse(json_data={"access_token": ACCESS_1, "expires_in": 3600}),
            FakeResponse(json_data={"access_token": ACCESS_2, "expires_in": 3600}),
        ]
        handler = _handlers(_spec(RECORDING_CODE, auth, [_tool("fetch")]))["fetch"]
        result = await handler(context={}, item="a")
        assert result == {"ok": True, "item": "a", "n": 2}
        assert len(http["calls"]) == 2  # initial fetch + one forced refresh

    @pytest.mark.asyncio
    async def test_authorization_code_cached_token_and_refresh_on_401_exception(
        self, client_env, http, auth_dir
    ):
        _save_token(auth_dir, "demo", ACCESS_1, REFRESH_1)
        code = (
            "import httpx\n"
            "seen = []\n"
            "async def fetch(context, access_token, item: str = ''):\n"
            "    seen.append(access_token)\n"
            "    if access_token == 'access-token-one-AAAA1111':\n"
            "        req = httpx.Request('GET', 'https://api.example.test/x')\n"
            "        raise httpx.HTTPStatusError('401', request=req, response=httpx.Response(401, request=req))\n"
            "    return {'ok': True, 'seen': list(seen)}\n"
        )
        http["responses"] = [
            FakeResponse(json_data={"access_token": ACCESS_2, "refresh_token": REFRESH_2, "expires_in": 3600}),
        ]
        result = await _handlers(_spec(code, AUTH_CODE, [_tool("fetch")]))["fetch"](context={})
        assert result == {"ok": True, "seen": [ACCESS_1, ACCESS_2]}
        assert http["calls"][0]["data"]["grant_type"] == "refresh_token"
        assert http["calls"][0]["data"]["refresh_token"] == REFRESH_1
        # Rotation: the newest refresh token is persisted.
        saved = json.loads((auth_dir / "demo.json").read_text())
        assert saved["refresh_token"] == REFRESH_2

    @pytest.mark.asyncio
    async def test_bearer_is_not_refreshed_or_retried(self, monkeypatch, http):
        monkeypatch.setenv("DEMO_BEARER", "bearer-value-1234")
        code = (
            "n = []\n"
            "async def fetch(context, token):\n"
            "    n.append(1)\n"
            "    return {'ok': False, 'status': 401}\n"
        )
        spec = _spec(code, {"type": "bearer", "inject_as": "token", "token_env": "DEMO_BEARER"}, [_tool("fetch")])
        handler = _handlers(spec)["fetch"]
        assert (await handler(context={}))["status"] == 401
        assert handler.__wrapped__.__globals__["n"] == [1]
        assert http["calls"] == []

    @pytest.mark.asyncio
    async def test_context_hook_and_existing_context_keys(self, monkeypatch):
        monkeypatch.setenv("DEMO_BEARER", "bearer-value-1234")
        code = (
            "async def fetch(context, token):\n"
            "    again = await context['mcpproxy_auth'].get_token()\n"
            "    return {'ok': True, 'same': again == token, 'tool': context.get('tool_name'),\n"
            "            'type': context['mcpproxy_auth'].type}\n"
        )
        spec = _spec(code, {"type": "bearer", "inject_as": "token", "token_env": "DEMO_BEARER"}, [_tool("fetch")])
        result = await _handlers(spec)["fetch"](context={"tool_name": "fetch"})
        assert result == {"ok": True, "same": True, "tool": "fetch", "type": "bearer"}

    @pytest.mark.asyncio
    async def test_end_to_end_through_register_tool_hides_the_argument(self, monkeypatch):
        """The injected argument never appears in the advertised schema."""
        from unittest.mock import patch

        monkeypatch.setenv("DEMO_BEARER", "bearer-value-1234")
        code = "async def fetch(context, token, item: str = ''):\n    return {'ok': True, 'got': bool(token)}\n"
        spec = _spec(code, {"type": "bearer", "inject_as": "token", "token_env": "DEMO_BEARER"}, [_tool("fetch")])
        tool_spec, handler = next(iter(server.build_tool_handlers(spec).values()))
        captured = {}

        def fake_tool(**kwargs):
            def deco(fn):
                captured["fn"] = fn
                return fn
            return deco

        with patch("server.mcp") as mcp:
            mcp.tool.side_effect = fake_tool
            server.register_tool(tool_spec, handler)
        fn = captured["fn"]
        assert "token" not in fn.__signature__.parameters
        assert await fn(None, item="x") == {"ok": True, "got": True}


# ---------------------------------------------------------------------------
# A1: per-tool opt-out of the 401 retry
# ---------------------------------------------------------------------------

class TestRetryOptOut:
    @pytest.mark.asyncio
    async def test_tool_level_opt_out_calls_once_without_refresh(self, client_env, http, auth_dir):
        _save_token(auth_dir, "demo", ACCESS_1, REFRESH_1)
        handler = _handlers(_spec(RECORDING_CODE, AUTH_CODE, [_tool("fetch", retry_on_401=False)]))["fetch"]
        result = await handler(context={})
        assert result["status"] == 401
        assert handler.__wrapped__.__globals__["calls"] == [ACCESS_1]
        assert http["calls"] == []  # no refresh request either

    @pytest.mark.asyncio
    async def test_provider_level_opt_out_and_tool_override(self, client_env, http, auth_dir):
        _save_token(auth_dir, "demo", ACCESS_1, REFRESH_1)
        auth = {**AUTH_CODE, "retry_on_401": False}
        tools = [_tool("fetch"), {**_tool("fetch"), "name": "fetch_retry", "retry_on_401": True}]
        handlers = _handlers(_spec(RECORDING_CODE, auth, tools))
        assert (await handlers["fetch"](context={}))["status"] == 401
        assert http["calls"] == []
        http["responses"] = [FakeResponse(json_data={"access_token": ACCESS_2, "expires_in": 3600})]
        assert (await handlers["fetch_retry"](context={}))["ok"] is True
        assert len(http["calls"]) == 1

    @pytest.mark.asyncio
    async def test_401_exception_with_opt_out_is_reraised(self, client_env, auth_dir):
        _save_token(auth_dir, "demo", ACCESS_1, REFRESH_1)
        code = (
            "class E(Exception):\n    status_code = 401\n"
            "async def fetch(context, access_token):\n    raise E('denied')\n"
        )
        handler = _handlers(_spec(code, AUTH_CODE, [_tool("fetch", retry_on_401=False)]))["fetch"]
        with pytest.raises(Exception, match="denied"):
            await handler(context={})


# ---------------------------------------------------------------------------
# authorization_required
# ---------------------------------------------------------------------------

class TestAuthorizationRequired:
    @pytest.mark.asyncio
    async def test_result_shape_and_banner(self, client_env, auth_dir):
        handler = _handlers(_spec(RECORDING_CODE, AUTH_CODE, [_tool("fetch")]))["fetch"]
        result = await handler(context={})
        assert result["ok"] is False
        assert result["status"] == "authorization_required"
        assert result["authorize_url"].startswith("https://auth.example.test/authorize?")
        assert result["tool"] == "fetch"
        assert result["manual_callback_required"] is False
        assert "message" in result
        assert pending_rest_auth["demo"] == result["authorize_url"]
        assert handler.__wrapped__.__globals__["calls"] == []  # handler never ran

    @pytest.mark.asyncio
    async def test_status_is_a_string_so_env_fallback_does_not_fire(self, client_env, auth_dir):
        result = await _handlers(_spec(RECORDING_CODE, AUTH_CODE, [_tool("fetch")]))["fetch"](context={})
        assert server._auth_failure_status(result) is None

    @pytest.mark.asyncio
    async def test_handler_hook_authorization_required_is_structured(self, client_env, auth_dir):
        code = (
            "async def fetch(context, access_token):\n"
            "    await context['mcpproxy_auth'].get_token(force_refresh=True)\n"
            "    return {'ok': True}\n"
        )
        _save_token(auth_dir, "demo", ACCESS_1, "")
        result = await _handlers(_spec(code, AUTH_CODE, [_tool("fetch")]))["fetch"](context={})
        assert result["status"] == "authorization_required"
        assert result["tool"] == "fetch"


# ---------------------------------------------------------------------------
# Per-provider redirect
# ---------------------------------------------------------------------------

class TestPerProviderRedirect:
    @pytest.mark.asyncio
    async def test_redirect_uri_is_used_stored_and_exchanged(self, client_env, http, auth_dir):
        auth = {**AUTH_CODE, "redirect_uri": "https://app.example.test/"}
        result = await _handlers(_spec(RECORDING_CODE, auth, [_tool("fetch")]))["fetch"](context={})
        assert result["manual_callback_required"] is True
        assert result["redirect_uri"] == "https://app.example.test/"
        query = parse_qs(urlsplit(result["authorize_url"]).query)
        assert query["redirect_uri"] == ["https://app.example.test/"]
        state = query["state"][0]
        assert AuthCodeTokenStore._pending_flows[state]["redirect_uri"] == "https://app.example.test/"

        http["responses"] = [FakeResponse(json_data={"access_token": ACCESS_2, "refresh_token": REFRESH_2})]
        await AuthCodeTokenStore.complete_authorization(state, "one-time-code")
        assert http["calls"][0]["data"]["redirect_uri"] == "https://app.example.test/"
        assert "demo" not in pending_rest_auth

    def test_redirect_uri_env(self, client_env, monkeypatch):
        monkeypatch.setenv("DEMO_REDIRECT", "https://other.example.test/cb")
        store = AuthCodeTokenStore("demo", {**AUTH_CODE, "redirect_uri_env": "DEMO_REDIRECT"})
        url = store.begin_authorization()
        assert parse_qs(urlsplit(url).query)["redirect_uri"] == ["https://other.example.test/cb"]

    def test_unset_redirect_keeps_the_global_default(self, client_env):
        url = AuthCodeTokenStore("demo", AUTH_CODE).begin_authorization()
        redirect = parse_qs(urlsplit(url).query)["redirect_uri"][0]
        assert redirect == rest_provider.oauth_redirect_uri()

    def test_rest_providers_accept_redirect_uri_too(self, client_env):
        auth = {k: v for k, v in AUTH_CODE.items() if k != "inject_as"}
        auth["redirect_uri"] = "https://app.example.test/"
        url = AuthCodeTokenStore("restprov", auth).begin_authorization()
        assert parse_qs(urlsplit(url).query)["redirect_uri"] == ["https://app.example.test/"]


# ---------------------------------------------------------------------------
# A2: clashes, and validation
# ---------------------------------------------------------------------------

class TestValidation:
    def test_inject_as_clashing_with_a_parameter_fails_setup(self, client_env):
        code = "async def fetch(context, item):\n    return {}\n"
        spec = _spec(code, {**AUTH_CODE, "inject_as": "item"}, [_tool("fetch")])
        with pytest.raises(ValueError, match="clashes with a parameter"):
            server.build_tool_handlers(spec)

    def test_inject_as_clashing_with_a_secret_argument_fails_setup(self, client_env):
        tool = _tool("fetch", secrets={"env": {"access_token": "SOME_ENV"}})
        spec = _spec("async def fetch(context, access_token):\n    return {}\n", AUTH_CODE, [tool])
        with pytest.raises(ValueError, match="clashes with a secrets argument"):
            server.build_tool_handlers(spec)

    def test_clash_on_another_tool_of_the_provider_is_caught(self):
        tools = [_tool("a"), {**_tool("b"), "input_schema": {"properties": {"access_token": {}}}}]
        errors = code_auth.validate_auth_config(AUTH_CODE, tools)
        assert any("tool 'b'" in e for e in errors)

    def test_other_rules(self):
        assert code_auth.validate_auth_config({"type": "nope", "inject_as": "t"}, [])
        assert any("redirect_uri" in e for e in code_auth.validate_auth_config(
            {**AUTH_CODE, "redirect_uri": "app.example.test"}, []))
        assert any("retry_on_401" in e for e in code_auth.validate_auth_config(
            {**AUTH_CODE, "retry_on_401": "no"}, []))
        assert any("mapping only for device_code" in e for e in code_auth.validate_auth_config(
            {**AUTH_CODE, "inject_as": {"a": "b"}}, []))
        assert code_auth.validate_auth_config(AUTH_CODE, [_tool("fetch")]) == []


# ---------------------------------------------------------------------------
# Secret hygiene
# ---------------------------------------------------------------------------

class TestScrubbing:
    @pytest.mark.asyncio
    async def test_token_in_handler_exception_is_scrubbed_and_status_kept(self, monkeypatch, capsys):
        monkeypatch.setenv("DEMO_BEARER", "bearer-value-1234")
        code = (
            "class E(Exception):\n    status_code = 403\n"
            "async def fetch(context, token):\n"
            "    raise E(f'GET https://api.example.test/?access_token={token} failed')\n"
        )
        spec = _spec(code, {"type": "bearer", "inject_as": "token", "token_env": "DEMO_BEARER"}, [_tool("fetch")])
        with pytest.raises(code_auth.ManagedAuthError) as info:
            await _handlers(spec)["fetch"](context={})
        assert "bearer-value-1234" not in str(info.value)
        assert "[REDACTED]" in str(info.value)
        assert info.value.status_code == 403
        assert "bearer-value-1234" not in capsys.readouterr().out

    @pytest.mark.asyncio
    async def test_tokens_never_printed_during_refresh(self, client_env, http, auth_dir, capsys):
        _save_token(auth_dir, "demo", ACCESS_1, REFRESH_1)
        http["responses"] = [FakeResponse(json_data={"access_token": ACCESS_2, "refresh_token": REFRESH_2})]
        await _handlers(_spec(RECORDING_CODE, AUTH_CODE, [_tool("fetch")]))["fetch"](context={})
        out = capsys.readouterr()
        for secret in (ACCESS_1, ACCESS_2, REFRESH_1, REFRESH_2, "client-secret-value"):
            assert secret not in out.out and secret not in out.err


# ---------------------------------------------------------------------------
# A5: startup warm-up
# ---------------------------------------------------------------------------

class TestWarmOnStart:
    def _write(self, config_dir: Path, name: str, spec: dict) -> None:
        import yaml

        (config_dir / f"{name}.yaml").write_text(yaml.safe_dump(spec))

    def _code_spec(self, **auth_extra):
        return {
            "auth": {**AUTH_CODE, **auth_extra},
            "code": "async def fetch(context, access_token):\n    return {}\n",
            "tools": [_tool("fetch")],
        }

    def _rest_spec(self, **auth_extra):
        auth = {k: v for k, v in AUTH_CODE.items() if k != "inject_as"}
        auth.update(auth_extra)
        return {
            "rest": {"base_url": "https://api.example.test", "auth": auth,
                     "endpoints": [{"name": "fetch", "method": "GET", "path": "/x"}]},
            "tools": [{"name": "fetch", "description": "x", "input_schema": {"type": "object", "properties": {}}}],
        }

    def test_code_providers_do_not_start_a_sign_in_at_boot(self, config_dir, client_env, auth_dir, monkeypatch):
        monkeypatch.setattr(server, "CONFIG_DIR", config_dir)
        self._write(config_dir, "quiet", self._code_spec())
        server._warm_rest_providers()
        assert pending_rest_auth == {}

    def test_code_provider_can_opt_in(self, config_dir, client_env, auth_dir, monkeypatch):
        monkeypatch.setattr(server, "CONFIG_DIR", config_dir)
        self._write(config_dir, "eager", self._code_spec(warm_on_start=True))
        server._warm_rest_providers()
        assert "eager" in pending_rest_auth

    def test_rest_default_unchanged_and_rest_can_opt_out(self, config_dir, client_env, auth_dir, monkeypatch):
        monkeypatch.setattr(server, "CONFIG_DIR", config_dir)
        self._write(config_dir, "restdefault", self._rest_spec())
        self._write(config_dir, "restquiet", self._rest_spec(warm_on_start=False))
        server._warm_rest_providers()
        assert "restdefault" in pending_rest_auth
        assert "restquiet" not in pending_rest_auth

    def test_existing_token_is_still_refreshed_silently(self, config_dir, client_env, auth_dir, http, monkeypatch):
        monkeypatch.setattr(server, "CONFIG_DIR", config_dir)
        self._write(config_dir, "quiet", self._code_spec())
        _save_token(auth_dir, "quiet", ACCESS_1, REFRESH_1, expires_in=-10)
        http["responses"] = [FakeResponse(json_data={"access_token": ACCESS_2, "refresh_token": REFRESH_2})]
        server._warm_rest_providers()
        assert json.loads((auth_dir / "quiet.json").read_text())["access_token"] == ACCESS_2
        assert pending_rest_auth == {}

    @pytest.mark.asyncio
    async def test_first_tool_call_starts_the_sign_in(self, client_env, auth_dir):
        assert pending_rest_auth == {}
        result = await _handlers(_spec(RECORDING_CODE, AUTH_CODE, [_tool("fetch")]))["fetch"](context={})
        assert result["status"] == "authorization_required"
        assert "demo" in pending_rest_auth


# ---------------------------------------------------------------------------
# Sign-in tools: auth_inject: false and complete_authorization
# ---------------------------------------------------------------------------

SIGN_IN_CODE = (
    "async def auth_status(context, reauthorize: bool = False):\n"
    "    auth = context['mcpproxy_auth']\n"
    "    return await (auth.login() if reauthorize else auth.status())\n"
    "async def complete_login(context, redirect_url: str):\n"
    "    return await context['mcpproxy_auth'].complete_authorization(redirect_url)\n"
)


def _sign_in_tools():
    return [
        {**_tool("auth_status"), "auth_inject": False,
         "input_schema": {"type": "object", "properties": {"reauthorize": {"type": "boolean"}}}},
        {**_tool("complete_login"), "auth_inject": False,
         "input_schema": {"type": "object", "properties": {"redirect_url": {"type": "string"}}}},
    ]


class TestSignInTools:
    @pytest.mark.asyncio
    async def test_auth_inject_false_runs_while_signed_out(self, client_env, auth_dir):
        handlers = _handlers(_spec(SIGN_IN_CODE, AUTH_CODE, _sign_in_tools()))
        status = await handlers["auth_status"](context={})
        assert status["signed_in"] is False
        assert pending_rest_auth == {}  # checking status starts nothing
        started = await handlers["auth_status"](context={}, reauthorize=True)
        assert started["status"] == "authorization_started"
        assert pending_rest_auth["demo"] == started["authorize_url"]

    @pytest.mark.asyncio
    async def test_complete_login_from_a_pasted_address(self, client_env, http, auth_dir, capsys):
        auth = {**AUTH_CODE, "redirect_uri": "https://app.example.test/"}
        handlers = _handlers(_spec(SIGN_IN_CODE, auth, _sign_in_tools()))
        url = (await handlers["auth_status"](context={}, reauthorize=True))["authorize_url"]
        state = parse_qs(urlsplit(url).query)["state"][0]
        http["responses"] = [FakeResponse(json_data={"access_token": ACCESS_2, "refresh_token": REFRESH_2})]
        code = "pasted-one-time-code-7777"
        result = await handlers["complete_login"](
            context={}, redirect_url=f"https://app.example.test/?code={code}&state={state}")
        assert result == {"ok": True, "status": "authorized"}
        assert http["calls"][0]["data"]["redirect_uri"] == "https://app.example.test/"
        assert json.loads((auth_dir / "demo.json").read_text())["access_token"] == ACCESS_2
        assert code not in capsys.readouterr().out

    @pytest.mark.asyncio
    async def test_complete_login_refuses_another_providers_state(self, client_env, auth_dir):
        other = AuthCodeTokenStore("other", AUTH_CODE).begin_authorization()
        state = parse_qs(urlsplit(other).query)["state"][0]
        handlers = _handlers(_spec(SIGN_IN_CODE, AUTH_CODE, _sign_in_tools()))
        result = await handlers["complete_login"](
            context={}, redirect_url=f"https://x.test/?code=secret-code-8888&state={state}")
        assert result["status"] == "unknown_state"
        assert "secret-code-8888" not in json.dumps(result)
        assert state in AuthCodeTokenStore._pending_flows

    @pytest.mark.asyncio
    async def test_complete_login_needs_code_and_state(self, client_env, auth_dir):
        handlers = _handlers(_spec(SIGN_IN_CODE, AUTH_CODE, _sign_in_tools()))
        result = await handlers["complete_login"](context={}, redirect_url="https://x.test/?code=only")
        assert result["status"] == "invalid_callback"

    @pytest.mark.asyncio
    async def test_failed_exchange_never_echoes_the_code(self, client_env, auth_dir, monkeypatch):
        handlers = _handlers(_spec(SIGN_IN_CODE, AUTH_CODE, _sign_in_tools()))
        url = (await handlers["auth_status"](context={}, reauthorize=True))["authorize_url"]
        state = parse_qs(urlsplit(url).query)["state"][0]

        class Boom:
            def __init__(self, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def post(self, url, data=None, **kw):
                raise RuntimeError(f"POST {url} code={data['code']}")

        monkeypatch.setattr(rest_provider.httpx, "AsyncClient", Boom)
        result = await handlers["complete_login"](
            context={}, redirect_url=f"https://x.test/?code=secret-code-9999&state={state}")
        assert result["status"] == "token_exchange_failed"
        assert "secret-code-9999" not in json.dumps(result)

    def test_auth_inject_must_be_boolean(self):
        tools = [{**_tool("a"), "auth_inject": "no"}]
        assert any("auth_inject" in e for e in code_auth.validate_auth_config(AUTH_CODE, tools))

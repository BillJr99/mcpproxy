"""Tests for caller-provided secret injection (``secrets.headers``).

Covers three layers separately:

* resolution  — ``plan_secret_injection`` / ``resolve_secret_defaults``
* invocation  — ``invoke_with_secret_fallback`` and ``_is_auth_failure``
* end-to-end  — the ``dynamic_tool`` closure built by ``register_tool``,
                including request isolation and secret hygiene of errors.
"""
import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

import server
from server import (
    SecretSource,
    _is_auth_failure,
    build_runtime_context,
    extract_request_headers,
    invoke_with_secret_fallback,
    plan_secret_injection,
    register_tool,
    resolve_secret_defaults,
)

HEADER_SECRET = "hdr-caller-secret-value-1234"
ENV_SECRET = "env-fallback-secret-value-5678"

BOTH = {"secrets": {
    "env": {"canvas_token": "CANVAS_API_KEY"},
    "headers": {"canvas_token": "X-MCPProxy-Canvas-Key"},
}}
HEADER_ONLY = {"secrets": {"headers": {"canvas_token": "X-MCPProxy-Canvas-Key"}}}
ENV_ONLY = {"secrets": {"env": {"canvas_token": "CANVAS_API_KEY"}}}


@pytest.fixture()
def env_secret(monkeypatch):
    monkeypatch.setenv("CANVAS_API_KEY", ENV_SECRET)


@pytest.fixture()
def no_env_secret(monkeypatch):
    monkeypatch.delenv("CANVAS_API_KEY", raising=False)


def _ctx(headers: dict[str, str] | None) -> Any:
    """A stand-in FastMCP Context whose request carries ``headers``."""
    request = SimpleNamespace(headers=headers) if headers is not None else None
    return SimpleNamespace(request_context=SimpleNamespace(request=request))


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

class TestResolution:
    def test_env_only_still_works(self, env_secret):
        assert resolve_secret_defaults(ENV_ONLY, {}) == {"canvas_token": ENV_SECRET}

    def test_env_only_ignores_unrelated_headers(self, env_secret):
        out = resolve_secret_defaults(ENV_ONLY, {}, {"x-mcpproxy-canvas-key": HEADER_SECRET})
        assert out == {"canvas_token": ENV_SECRET}

    def test_header_only_works(self, no_env_secret):
        out = resolve_secret_defaults(HEADER_ONLY, {}, {"X-MCPProxy-Canvas-Key": HEADER_SECRET})
        assert out == {"canvas_token": HEADER_SECRET}

    def test_header_preferred_over_env(self, env_secret):
        plan = plan_secret_injection(BOTH, {}, {"X-MCPProxy-Canvas-Key": HEADER_SECRET})
        assert plan.primary_kwargs == {"canvas_token": HEADER_SECRET}
        assert plan.fallback_kwargs == {"canvas_token": ENV_SECRET}
        assert plan.sources["canvas_token"] == {
            "primary": SecretSource("header", "X-MCPProxy-Canvas-Key"),
            "fallback": SecretSource("env", "CANVAS_API_KEY"),
        }

    def test_missing_header_falls_back_to_env(self, env_secret):
        plan = plan_secret_injection(BOTH, {}, {})
        assert plan.primary_kwargs == {"canvas_token": ENV_SECRET}
        assert plan.fallback_kwargs is None  # nothing to fall back from

    def test_no_headers_argument_falls_back_to_env(self, env_secret):
        assert resolve_secret_defaults(BOTH, {}) == {"canvas_token": ENV_SECRET}

    @pytest.mark.parametrize("blank", ["", "   ", "\t \n"])
    def test_blank_header_falls_back_to_env(self, env_secret, blank):
        plan = plan_secret_injection(BOTH, {}, {"X-MCPProxy-Canvas-Key": blank})
        assert plan.primary_kwargs == {"canvas_token": ENV_SECRET}
        assert plan.fallback_kwargs is None

    @pytest.mark.parametrize("name", [
        "x-mcpproxy-canvas-key", "X-MCPPROXY-CANVAS-KEY", "x-MCPProxy-canvas-KEY",
    ])
    def test_header_lookup_case_insensitive(self, env_secret, name):
        out = resolve_secret_defaults(BOTH, {}, {name: HEADER_SECRET})
        assert out == {"canvas_token": HEADER_SECRET}

    def test_configured_header_name_case_insensitive(self, env_secret):
        tool = {"secrets": {"headers": {"canvas_token": "x-mcpproxy-CANVAS-key"}}}
        out = resolve_secret_defaults(tool, {}, {"X-MCPProxy-Canvas-Key": HEADER_SECRET})
        assert out == {"canvas_token": HEADER_SECRET}

    def test_header_value_whitespace_trimmed(self, no_env_secret):
        out = resolve_secret_defaults(HEADER_ONLY, {}, {"x-mcpproxy-canvas-key": f"  {HEADER_SECRET} "})
        assert out == {"canvas_token": HEADER_SECRET}

    def test_missing_both_raises_naming_sources_only(self, no_env_secret):
        with pytest.raises(RuntimeError) as err:
            resolve_secret_defaults(BOTH, {}, {"Other-Header": HEADER_SECRET})
        assert "X-MCPProxy-Canvas-Key" in str(err.value)
        assert "CANVAS_API_KEY" in str(err.value)
        assert HEADER_SECRET not in str(err.value)

    def test_missing_header_only_raises(self, no_env_secret):
        with pytest.raises(RuntimeError, match="X-MCPProxy-Canvas-Key"):
            resolve_secret_defaults(HEADER_ONLY, {}, {})

    def test_missing_env_only_message_unchanged(self, no_env_secret):
        with pytest.raises(RuntimeError, match="^Missing required secret environment variable: CANVAS_API_KEY$"):
            resolve_secret_defaults(ENV_ONLY, {}, {"x-mcpproxy-canvas-key": HEADER_SECRET})

    def test_empty_env_with_header_uses_header_without_fallback(self, monkeypatch):
        monkeypatch.setenv("CANVAS_API_KEY", "")
        plan = plan_secret_injection(BOTH, {}, {"x-mcpproxy-canvas-key": HEADER_SECRET})
        assert plan.primary_kwargs == {"canvas_token": HEADER_SECRET}
        assert plan.fallback_kwargs is None

    def test_non_secret_kwargs_untouched(self, env_secret):
        kwargs = {"course_id": "CS357", "limit": 0, "flag": False, "text": ""}
        out = resolve_secret_defaults(BOTH, kwargs, {"x-mcpproxy-canvas-key": HEADER_SECRET})
        assert out == {**kwargs, "canvas_token": HEADER_SECRET}
        assert "canvas_token" not in kwargs  # input not mutated

    def test_none_optionals_omitted(self, env_secret):
        out = resolve_secret_defaults(BOTH, {"offset": None, "q": "x"}, {"x-mcpproxy-canvas-key": HEADER_SECRET})
        assert out == {"q": "x", "canvas_token": HEADER_SECRET}
        plan = plan_secret_injection(BOTH, {"offset": None}, {"x-mcpproxy-canvas-key": HEADER_SECRET})
        assert "offset" not in plan.fallback_kwargs

    def test_headers_mapping_must_be_dict(self):
        with pytest.raises(RuntimeError, match="secrets.headers"):
            resolve_secret_defaults({"secrets": {"headers": ["X-Key"]}}, {})

    def test_multiple_header_args_all_swapped_on_fallback(self, monkeypatch):
        monkeypatch.setenv("KEY_A", "env-a-value")
        monkeypatch.setenv("KEY_B", "env-b-value")
        monkeypatch.setenv("KEY_C", "env-c-value")
        tool = {"secrets": {
            "env": {"a": "KEY_A", "b": "KEY_B", "c": "KEY_C"},
            "headers": {"a": "X-A", "b": "X-B", "c": "X-C"},
        }}
        plan = plan_secret_injection(tool, {}, {"x-a": "hdr-a-value", "x-b": "hdr-b-value"})
        assert plan.primary_kwargs == {"a": "hdr-a-value", "b": "hdr-b-value", "c": "env-c-value"}
        assert plan.fallback_kwargs == {"a": "env-a-value", "b": "env-b-value", "c": "env-c-value"}

    def test_partial_fallback_disables_retry(self, monkeypatch):
        monkeypatch.setenv("KEY_A", "env-a-value")
        monkeypatch.delenv("KEY_B", raising=False)
        tool = {"secrets": {"env": {"a": "KEY_A", "b": "KEY_B"}, "headers": {"a": "X-A", "b": "X-B"}}}
        plan = plan_secret_injection(tool, {}, {"x-a": "hdr-a-value", "x-b": "hdr-b-value"})
        assert plan.primary_kwargs == {"a": "hdr-a-value", "b": "hdr-b-value"}
        assert plan.fallback_kwargs is None

    def test_plan_repr_hides_values(self, env_secret):
        plan = plan_secret_injection(BOTH, {}, {"x-mcpproxy-canvas-key": HEADER_SECRET})
        text = repr(plan)
        assert HEADER_SECRET not in text and ENV_SECRET not in text
        assert "X-MCPProxy-Canvas-Key" in text


# ---------------------------------------------------------------------------
# Header extraction
# ---------------------------------------------------------------------------

class TestExtractRequestHeaders:
    def test_none_ctx(self):
        assert extract_request_headers(None) == {}

    def test_names_lowercased(self):
        out = extract_request_headers(_ctx({"X-MCPProxy-Canvas-Key": "v", "Accept": "a"}))
        assert out == {"x-mcpproxy-canvas-key": "v", "accept": "a"}

    def test_starlette_headers(self):
        from starlette.datastructures import Headers
        headers = Headers(raw=[(b"x-mcpproxy-canvas-key", b"v1")])
        assert extract_request_headers(_ctx(headers)) == {"x-mcpproxy-canvas-key": "v1"}

    def test_no_request_falls_back_to_fastmcp_helper(self):
        with patch("fastmcp.server.dependencies.get_http_headers", return_value={"X-K": "v"}) as helper:
            assert extract_request_headers(_ctx(None)) == {"x-k": "v"}
        helper.assert_called_once_with(include_all=True)

    def test_no_request_and_no_http_context_is_empty(self):
        # Outside an HTTP request FastMCP's helper returns {} rather than raising.
        assert extract_request_headers(_ctx(None)) == {}

    def test_request_context_raising_degrades_safely(self):
        class Ctx:
            @property
            def request_context(self):
                raise ValueError(f"boom {HEADER_SECRET}")
        assert extract_request_headers(Ctx()) == {}

    def test_unreadable_headers_degrade_safely(self, capsys):
        class BadHeaders:
            def items(self):
                raise RuntimeError(f"cannot read {HEADER_SECRET}")
        assert extract_request_headers(_ctx(BadHeaders())) == {}
        assert HEADER_SECRET not in capsys.readouterr().out

    def test_magicmock_ctx_yields_empty(self):
        assert extract_request_headers(MagicMock()) == {}


# ---------------------------------------------------------------------------
# Authentication-failure detection
# ---------------------------------------------------------------------------

class _StatusError(Exception):
    def __init__(self, status_code):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


def _httpx_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://canvas.example/api/v1/courses")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(f"HTTP {status}", request=request, response=response)


class TestIsAuthFailure:
    @pytest.mark.parametrize("status", [401, 403])
    def test_structured_status(self, status):
        assert _is_auth_failure({"ok": False, "status": status})
        assert _is_auth_failure({"status_code": status})

    @pytest.mark.parametrize("status", [401, 403])
    def test_exception_status(self, status):
        assert _is_auth_failure(_StatusError(status))
        assert _is_auth_failure(_httpx_error(status))

    def test_exception_status_attr(self):
        exc = Exception("x")
        exc.status = 403
        assert _is_auth_failure(exc)

    @pytest.mark.parametrize("status", [400, 404, 429, 500, 502])
    def test_other_statuses_not_auth(self, status):
        assert not _is_auth_failure({"ok": False, "status": status})
        assert not _is_auth_failure(_httpx_error(status))

    @pytest.mark.parametrize("value", [
        {"ok": False},
        {"ok": False, "error": "401 Unauthorized"},
        {"ok": False, "error": "Forbidden"},
        {"ok": False, "status": "401"},
        {"ok": False, "status": True},
        {"ok": True, "status": 401},
        "unauthorized",
        None,
        [{"status": 401}],
        RuntimeError("401 Unauthorized"),
        TimeoutError("timed out"),
    ])
    def test_not_auth_failure(self, value):
        assert not _is_auth_failure(value)


# ---------------------------------------------------------------------------
# Invocation and one-time fallback
# ---------------------------------------------------------------------------

class _Recorder:
    """An async handler returning/raising queued outcomes and logging tokens."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.tokens: list[str] = []

    async def __call__(self, context, **kwargs):
        self.tokens.append(kwargs.get("canvas_token"))
        outcome = self.outcomes.pop(0) if self.outcomes else {"ok": True}
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


async def _invoke(handler, headers, tool=BOTH):
    plan = plan_secret_injection(tool, {}, headers)
    return await invoke_with_secret_fallback(handler, build_runtime_context({"name": "t"}, None), plan, "t")


HDR = {"X-MCPProxy-Canvas-Key": HEADER_SECRET}


class TestInvocationFallback:
    @pytest.mark.asyncio
    async def test_valid_header_executes_once(self, env_secret):
        h = _Recorder({"ok": True, "data": 1})
        assert await _invoke(h, HDR) == {"ok": True, "data": 1}
        assert h.tokens == [HEADER_SECRET]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [401, 403])
    async def test_auth_result_retries_once_with_env(self, env_secret, status):
        h = _Recorder({"ok": False, "status": status}, {"ok": True, "via": "env"})
        assert await _invoke(h, HDR) == {"ok": True, "via": "env"}
        assert h.tokens == [HEADER_SECRET, ENV_SECRET]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [401, 403])
    async def test_auth_exception_retries_once_with_env(self, env_secret, status):
        h = _Recorder(_httpx_error(status), {"ok": True, "via": "env"})
        assert await _invoke(h, HDR) == {"ok": True, "via": "env"}
        assert h.tokens == [HEADER_SECRET, ENV_SECRET]

    @pytest.mark.asyncio
    async def test_failed_fallback_returned_without_third_attempt(self, env_secret):
        final = {"ok": False, "status": 401, "error": "HTTP 401"}
        h = _Recorder({"ok": False, "status": 403}, final, {"ok": True})
        assert await _invoke(h, HDR) == final
        assert h.tokens == [HEADER_SECRET, ENV_SECRET]

    @pytest.mark.asyncio
    async def test_failed_fallback_exception_propagates_without_third_attempt(self, env_secret):
        h = _Recorder(_StatusError(401), _StatusError(401), {"ok": True})
        with pytest.raises(_StatusError):
            await _invoke(h, HDR)
        assert h.tokens == [HEADER_SECRET, ENV_SECRET]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [400, 404, 429, 500])
    async def test_non_auth_status_does_not_retry(self, env_secret, status):
        result = {"ok": False, "status": status}
        h = _Recorder(result)
        assert await _invoke(h, HDR) == result
        assert h.tokens == [HEADER_SECRET]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("exc", [
        httpx.ConnectTimeout("timed out"),
        httpx.ConnectError("connection refused"),
        asyncio.TimeoutError(),
        ValueError("validation failed"),
        RuntimeError("401 Unauthorized"),  # string only, no explicit status
    ])
    async def test_exceptions_do_not_retry(self, env_secret, exc):
        h = _Recorder(exc)
        with pytest.raises(type(exc)):
            await _invoke(h, HDR)
        assert h.tokens == [HEADER_SECRET]

    @pytest.mark.asyncio
    async def test_ok_false_without_status_does_not_retry(self, env_secret):
        h = _Recorder({"ok": False, "error": "Unauthorized"})
        assert await _invoke(h, HDR) == {"ok": False, "error": "Unauthorized"}
        assert h.tokens == [HEADER_SECRET]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [401, 403])
    async def test_header_without_fallback_does_not_retry(self, no_env_secret, status):
        result = {"ok": False, "status": status}
        h = _Recorder(result)
        assert await _invoke(h, HDR) == result
        assert h.tokens == [HEADER_SECRET]

    @pytest.mark.asyncio
    async def test_env_credential_401_does_not_retry(self, env_secret):
        result = {"ok": False, "status": 401}
        h = _Recorder(result)
        assert await _invoke(h, {}) == result
        assert h.tokens == [ENV_SECRET]

    @pytest.mark.asyncio
    async def test_fallback_log_names_sources_only(self, env_secret, capsys):
        h = _Recorder({"ok": False, "status": 401}, {"ok": True})
        await _invoke(h, HDR)
        out = capsys.readouterr().out
        assert "X-MCPProxy-Canvas-Key" in out and "CANVAS_API_KEY" in out
        assert HEADER_SECRET not in out and ENV_SECRET not in out


# ---------------------------------------------------------------------------
# End-to-end through register_tool's dynamic_tool closure
# ---------------------------------------------------------------------------

def _register(tool_spec, handler):
    captured = {}

    def fake_tool_decorator(**kwargs):
        def decorator(fn):
            captured["fn"] = fn
            return fn
        return decorator

    with patch("server.mcp") as mock_mcp:
        mock_mcp.tool.side_effect = fake_tool_decorator
        register_tool(tool_spec, handler)
    return captured["fn"]


CANVAS_TOOL = {
    "name": "canvas_list_courses",
    "description": "List courses.",
    "input_schema": {
        "type": "object",
        "properties": {"course_id": {"type": "string"}},
        "required": ["course_id"],
    },
    **BOTH,
}


class TestDynamicTool:
    def test_secret_absent_from_schema(self):
        fn = _register(CANVAS_TOOL, _Recorder())
        assert "canvas_token" not in fn.__signature__.parameters
        assert "canvas_token" not in fn.__annotations__

    @pytest.mark.asyncio
    async def test_header_used_and_ctx_preserved(self, env_secret):
        seen = {}

        async def handler(context, **kwargs):
            seen.update(context=context, kwargs=kwargs)
            return {"ok": True}

        fn = _register(CANVAS_TOOL, handler)
        ctx = _ctx({"x-mcpproxy-canvas-key": HEADER_SECRET})
        assert await fn(ctx, course_id="CS357") == {"ok": True}
        assert seen["kwargs"] == {"course_id": "CS357", "canvas_token": HEADER_SECRET}
        assert seen["context"]["mcp_context"] is ctx

    @pytest.mark.asyncio
    async def test_concurrent_requests_isolated(self, env_secret):
        """Caller A sends the header, caller B does not; neither sees the other's secret."""
        seen: dict[str, list[str]] = {}
        both_started = asyncio.Event()
        started = 0

        async def handler(context, course_id, canvas_token):
            nonlocal started
            started += 1
            if started == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=2)  # force interleaving
            seen.setdefault(course_id, []).append(canvas_token)
            return {"ok": True, "course": course_id}

        fn = _register(CANVAS_TOOL, handler)
        ctx_a = _ctx({"X-MCPProxy-Canvas-Key": HEADER_SECRET})
        ctx_b = _ctx({"User-Agent": "client-b"})
        await asyncio.gather(fn(ctx_a, course_id="A"), fn(ctx_b, course_id="B"))
        assert seen == {"A": [HEADER_SECRET], "B": [ENV_SECRET]}
        # A later call from B still cannot observe A's credential.
        await fn(ctx_b, course_id="B")
        assert seen["B"] == [ENV_SECRET, ENV_SECRET]

    @pytest.mark.asyncio
    async def test_retry_through_dynamic_tool(self, env_secret):
        h = _Recorder({"ok": False, "status": 401}, {"ok": True})
        fn = _register(CANVAS_TOOL, h)
        assert await fn(_ctx(HDR), course_id="X") == {"ok": True}
        assert h.tokens == [HEADER_SECRET, ENV_SECRET]

    @pytest.mark.asyncio
    async def test_missing_secret_error_names_sources_only(self, no_env_secret):
        fn = _register(CANVAS_TOOL, _Recorder())
        result = await fn(_ctx({}), course_id="X")
        assert result["ok"] is False
        assert "X-MCPProxy-Canvas-Key" in result["error"]
        assert "CANVAS_API_KEY" in result["error"]

    @pytest.mark.asyncio
    async def test_handler_error_echoing_secret_is_scrubbed(self, env_secret, capsys):
        async def leaky(context, **kwargs):
            raise RuntimeError(f"request to https://canvas/?access_token={kwargs['canvas_token']} failed")

        fn = _register(CANVAS_TOOL, leaky)
        result = await fn(_ctx(HDR), course_id="X")
        assert result["ok"] is False
        assert HEADER_SECRET not in result["error"]
        assert "[REDACTED]" in result["error"]
        captured = capsys.readouterr()
        assert HEADER_SECRET not in captured.out
        assert HEADER_SECRET not in captured.err

    @pytest.mark.asyncio
    async def test_fallback_error_echoing_env_secret_is_scrubbed(self, env_secret, capsys):
        h = _Recorder(_StatusError(401), RuntimeError(f"bad token {ENV_SECRET}"))
        fn = _register(CANVAS_TOOL, h)
        result = await fn(_ctx(HDR), course_id="X")
        assert ENV_SECRET not in result["error"] and HEADER_SECRET not in result["error"]
        captured = capsys.readouterr()
        assert ENV_SECRET not in captured.out + captured.err

    @pytest.mark.asyncio
    async def test_rest_invoke_path_with_no_ctx_uses_env(self, env_secret):
        h = _Recorder()
        fn = _register(CANVAS_TOOL, h)
        assert await fn(None, course_id="X") == {"ok": True}
        assert h.tokens == [ENV_SECRET]

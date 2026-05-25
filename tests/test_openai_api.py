"""Tests for the OpenAI-compatible /v1/tools endpoints in frontend/app.py.

These endpoints are a pure addition — they share the tool_registry with server.py
but don't touch the MCP protocol at all.

Each test pre-populates tool_registry directly (then clears it in teardown) and
creates a fresh TestClient against the app.  No server.py import side-effects are
involved because we never register tools via server.register_tool() here.
"""
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import tool_registry
from frontend.app import create_app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clean_registry():
    """Ensure a clean tool_registry before and after every test."""
    tool_registry.clear()
    yield
    tool_registry.clear()


@pytest.fixture()
def app(tmp_path: Path):
    return create_app(
        config_dir=tmp_path / "tools",
        env_file=tmp_path / ".env",
    )


@pytest.fixture()
def client(app):
    return TestClient(app)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _register_tool(
    name: str,
    description: str = "A test tool",
    input_schema: dict | None = None,
    handler=None,
):
    """Register a fake tool in the registry."""
    if input_schema is None:
        input_schema = {
            "type": "object",
            "properties": {
                "msg": {"type": "string", "description": "A message"},
            },
            "required": ["msg"],
        }
    if handler is None:
        handler = AsyncMock(return_value={"ok": True, "echo": "hello"})
    spec = {
        "name": name,
        "description": description,
        "input_schema": input_schema,
    }
    tool_registry.register(name, spec, handler)
    return handler


# ---------------------------------------------------------------------------
# GET /v1/tools
# ---------------------------------------------------------------------------

class TestListOpenAITools:
    def test_empty_registry_returns_empty_list(self, client):
        resp = client.get("/v1/tools")
        assert resp.status_code == 200
        body = resp.json()
        assert body == {"tools": []}

    def test_single_tool_returned_in_openai_schema(self, client):
        _register_tool("myprov__ping", description="Ping the server")
        resp = client.get("/v1/tools")
        assert resp.status_code == 200
        tools = resp.json()["tools"]
        assert len(tools) == 1
        tool = tools[0]
        assert tool["type"] == "function"
        fn = tool["function"]
        assert fn["name"] == "myprov__ping"
        assert fn["description"] == "Ping the server"
        assert "parameters" in fn

    def test_parameters_match_input_schema(self, client):
        schema = {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The URL"},
                "timeout": {"type": "integer"},
            },
            "required": ["url"],
        }
        _register_tool("browser__navigate", input_schema=schema)
        resp = client.get("/v1/tools")
        fn = resp.json()["tools"][0]["function"]
        params = fn["parameters"]
        assert params["properties"]["url"]["type"] == "string"
        assert params["properties"]["timeout"]["type"] == "integer"
        assert "url" in params["required"]

    def test_multiple_tools_all_returned(self, client):
        _register_tool("p__a", description="Tool A")
        _register_tool("p__b", description="Tool B")
        _register_tool("p__c", description="Tool C")
        resp = client.get("/v1/tools")
        names = {t["function"]["name"] for t in resp.json()["tools"]}
        assert names == {"p__a", "p__b", "p__c"}

    def test_tool_with_no_input_schema_gets_empty_schema(self, client):
        """A spec without input_schema should produce a valid (empty) parameters object."""
        tool_registry.register(
            "bare__tool",
            {"name": "bare__tool", "description": "no schema"},
            AsyncMock(return_value="ok"),
        )
        resp = client.get("/v1/tools")
        assert resp.status_code == 200
        fn = resp.json()["tools"][0]["function"]
        params = fn["parameters"]
        assert params["type"] == "object"
        assert params["properties"] == {}
        assert params["required"] == []

    def test_response_structure_has_required_fields(self, client):
        _register_tool("p__x")
        body = client.get("/v1/tools").json()
        assert "tools" in body
        for t in body["tools"]:
            assert t["type"] == "function"
            assert "function" in t
            assert "name" in t["function"]
            assert "description" in t["function"]
            assert "parameters" in t["function"]


# ---------------------------------------------------------------------------
# POST /v1/tools/{tool_name}/invoke
# ---------------------------------------------------------------------------

class TestInvokeOpenAITool:

    # ── 404 when tool not found ──────────────────────────────────────────────

    def test_unknown_tool_returns_404(self, client):
        resp = client.post("/v1/tools/does_not_exist/invoke", json={"arguments": {}})
        assert resp.status_code == 404

    # ── Successful invocations ───────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_handler_called_with_arguments(self, client):
        handler = AsyncMock(return_value={"ok": True})
        _register_tool("p__echo", handler=handler)
        client.post("/v1/tools/p__echo/invoke", json={"arguments": {"msg": "hi"}})
        handler.assert_awaited_once()
        _, kwargs = handler.call_args
        assert kwargs.get("msg") == "hi"

    @pytest.mark.asyncio
    async def test_ctx_passed_as_none(self, client):
        """The handler must receive ctx=None (first positional arg)."""
        received_ctx = []

        async def capturing_handler(ctx, **kwargs):
            received_ctx.append(ctx)
            return {"ok": True}

        tool_registry.register(
            "p__ctx_check",
            {"name": "p__ctx_check", "description": "x", "input_schema": {"type": "object", "properties": {}, "required": []}},
            capturing_handler,
        )
        client.post("/v1/tools/p__ctx_check/invoke", json={"arguments": {}})
        assert received_ctx == [None]

    def test_success_response_shape(self, client):
        _register_tool("p__t", handler=AsyncMock(return_value={"ok": True}))
        resp = client.post("/v1/tools/p__t/invoke", json={"arguments": {"msg": "x"}})
        assert resp.status_code == 200
        body = resp.json()
        assert body["type"] == "tool_result"
        assert body["is_error"] is False
        assert isinstance(body["content"], list)

    # ── Result normalisation ─────────────────────────────────────────────────

    def test_string_result_wrapped_in_content_array(self, client):
        _register_tool("p__t", handler=AsyncMock(return_value="hello world"))
        body = client.post("/v1/tools/p__t/invoke", json={"arguments": {"msg": "x"}}).json()
        assert body["content"] == [{"type": "text", "text": "hello world"}]
        assert body["is_error"] is False

    def test_dict_with_content_key_passed_through(self, client):
        content_val = [{"type": "text", "text": "rich result"}]
        _register_tool("p__t", handler=AsyncMock(return_value={"content": content_val, "extra": 1}))
        body = client.post("/v1/tools/p__t/invoke", json={"arguments": {"msg": "x"}}).json()
        assert body["content"] == content_val

    def test_dict_without_content_key_serialised_as_text(self, client):
        _register_tool("p__t", handler=AsyncMock(return_value={"ok": True, "data": [1, 2, 3]}))
        body = client.post("/v1/tools/p__t/invoke", json={"arguments": {"msg": "x"}}).json()
        assert len(body["content"]) == 1
        parsed = json.loads(body["content"][0]["text"])
        assert parsed == {"ok": True, "data": [1, 2, 3]}

    def test_list_result_used_as_content_directly(self, client):
        content = [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]
        _register_tool("p__t", handler=AsyncMock(return_value=content))
        body = client.post("/v1/tools/p__t/invoke", json={"arguments": {"msg": "x"}}).json()
        assert body["content"] == content

    # ── Error handling ───────────────────────────────────────────────────────

    def test_handler_exception_returns_is_error_true(self, client):
        async def bad_handler(ctx, **kwargs):
            raise ValueError("something exploded")

        tool_registry.register(
            "p__bad",
            {"name": "p__bad", "description": "x", "input_schema": {"type": "object", "properties": {}, "required": []}},
            bad_handler,
        )
        resp = client.post("/v1/tools/p__bad/invoke", json={"arguments": {}})
        assert resp.status_code == 200
        body = resp.json()
        assert body["type"] == "tool_result"
        assert body["is_error"] is True
        assert "something exploded" in body["content"][0]["text"]

    def test_handler_exception_does_not_return_500(self, client):
        """Errors are surfaced as tool results (HTTP 200), not server errors."""
        async def raising(ctx, **kwargs):
            raise RuntimeError("oops")

        tool_registry.register(
            "p__raise",
            {"name": "p__raise", "description": "x", "input_schema": {"type": "object", "properties": {}, "required": []}},
            raising,
        )
        resp = client.post("/v1/tools/p__raise/invoke", json={"arguments": {}})
        assert resp.status_code == 200

    def test_empty_arguments_body_accepted(self, client):
        _register_tool("p__t", handler=AsyncMock(return_value="ok"))
        resp = client.post("/v1/tools/p__t/invoke", json={})
        assert resp.status_code == 200

    def test_missing_body_treated_as_no_arguments(self, client):
        """Sending no body (or a non-JSON body) should be treated as {} arguments
        rather than crashing — the endpoint is lenient about the request body."""
        _register_tool("p__t", handler=AsyncMock(return_value="ok"))
        resp = client.post("/v1/tools/p__t/invoke")
        assert resp.status_code == 200
        body = resp.json()
        assert body["is_error"] is False

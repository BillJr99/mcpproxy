"""Unit tests for server.py pure helper functions.

Note: server.py has module-level side effects (load_provider_specs + register_provider).
conftest.py sets MCP_TOOL_CONFIG_DIR to an empty temp dir before import so zero tools
are registered and no npx processes are started.
"""
import inspect
import os
import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

# server.py has module-level side effects (load_provider_specs + register_provider).
# conftest.py has already set MCP_TOOL_CONFIG_DIR to an empty temp dir, so the
# import is safe and results in zero tools being registered.
from server import (
    _build_typed_signature,
    build_runtime_context,
    exec_provider_code,
    load_provider_specs,
    redact_secrets,
    register_tool,
    resolve_env_defaults,
)


# ---------------------------------------------------------------------------
# redact_secrets
# ---------------------------------------------------------------------------

class TestRedactSecrets:
    def test_plain_dict_no_secrets(self):
        d = {"name": "alice", "score": 42}
        assert redact_secrets(d) == d

    @pytest.mark.parametrize("key", ["password", "token", "secret", "api_key", "apikey", "authorization"])
    def test_known_secret_key_redacted(self, key):
        result = redact_secrets({key: "supersecret"})
        assert result[key] == "[REDACTED]"

    def test_partial_match_in_key(self):
        result = redact_secrets({"my_api_key_here": "val"})
        assert result["my_api_key_here"] == "[REDACTED]"

    def test_nested_dict(self):
        result = redact_secrets({"outer": {"password": "secret", "name": "bob"}})
        assert result["outer"]["password"] == "[REDACTED]"
        assert result["outer"]["name"] == "bob"

    def test_list_of_dicts(self):
        result = redact_secrets([{"token": "abc"}, {"name": "x"}])
        assert result[0]["token"] == "[REDACTED]"
        assert result[1]["name"] == "x"

    def test_scalar_passthrough(self):
        assert redact_secrets(42) == 42
        assert redact_secrets("hello") == "hello"
        assert redact_secrets(None) is None

    def test_case_insensitive(self):
        result = redact_secrets({"PASSWORD": "secret"})
        assert result["PASSWORD"] == "[REDACTED]"


# ---------------------------------------------------------------------------
# load_provider_specs
# ---------------------------------------------------------------------------

class TestLoadProviderSpecs:
    def test_empty_dir_returns_empty(self, config_dir: Path):
        assert load_provider_specs(config_dir) == []

    def test_missing_dir_returns_empty(self, tmp_path: Path):
        assert load_provider_specs(tmp_path / "nonexistent") == []

    def test_single_yaml_loaded(self, config_dir: Path, simple_yaml_spec: dict):
        p = config_dir / "mytools.yaml"
        p.write_text(yaml.dump(simple_yaml_spec))
        specs = load_provider_specs(config_dir)
        assert len(specs) == 1
        assert specs[0]["tools"][0]["name"] == "ping"
        assert specs[0]["_config_path"] == str(p)

    def test_multiple_yamls_sorted_alphabetically(self, config_dir: Path):
        for name in ("z_last.yaml", "a_first.yaml", "m_mid.yaml"):
            (config_dir / name).write_text("code: ''\ntools: []")
        specs = load_provider_specs(config_dir)
        stems = [Path(s["_config_path"]).stem for s in specs]
        assert stems == sorted(stems)

    def test_non_yaml_files_ignored(self, config_dir: Path):
        (config_dir / "readme.txt").write_text("hello")
        (config_dir / "mytools.yaml").write_text("code: ''\ntools: []")
        assert len(load_provider_specs(config_dir)) == 1

    def test_invalid_yaml_raises(self, config_dir: Path):
        (config_dir / "bad.yaml").write_text(": : : bad yaml {{{{")
        with pytest.raises(Exception):
            load_provider_specs(config_dir)


# ---------------------------------------------------------------------------
# exec_provider_code
# ---------------------------------------------------------------------------

class TestExecProviderCode:
    def test_empty_code_returns_empty_namespace(self):
        assert exec_provider_code({"code": ""}) == {}
        assert exec_provider_code({}) == {}

    def test_defines_function_in_namespace(self, simple_yaml_spec: dict):
        ns = exec_provider_code(simple_yaml_spec)
        assert "ping" in ns
        assert callable(ns["ping"])

    def test_runtime_import_available(self):
        spec = {"code": "import json\nresult = json.dumps({'a': 1})"}
        ns = exec_provider_code(spec)
        assert ns["result"] == '{"a": 1}'

    def test_syntax_error_raises(self):
        with pytest.raises(SyntaxError):
            exec_provider_code({"code": "def broken(: pass"})

    def test_runtime_error_raises(self):
        with pytest.raises(ZeroDivisionError):
            exec_provider_code({"code": "x = 1/0"})

    def test_source_path_in_traceback(self, tmp_path: Path):
        spec = {"code": "raise ValueError('oops')", "_config_path": str(tmp_path / "test.yaml")}
        with pytest.raises(ValueError):
            exec_provider_code(spec)


# ---------------------------------------------------------------------------
# resolve_env_defaults
# ---------------------------------------------------------------------------

class TestResolveEnvDefaults:
    def test_no_secrets_returns_original(self):
        result = resolve_env_defaults({}, {"x": 1})
        assert result == {"x": 1}

    def test_secret_injected_from_env(self, monkeypatch):
        monkeypatch.setenv("MY_API_KEY", "abc123")
        tool = {"secrets": {"env": {"api_key": "MY_API_KEY"}}}
        result = resolve_env_defaults(tool, {})
        assert result["api_key"] == "abc123"

    def test_missing_env_var_raises(self, monkeypatch):
        monkeypatch.delenv("MISSING_VAR", raising=False)
        tool = {"secrets": {"env": {"token": "MISSING_VAR"}}}
        with pytest.raises(RuntimeError, match="MISSING_VAR"):
            resolve_env_defaults(tool, {})

    def test_existing_kwargs_preserved(self, monkeypatch):
        monkeypatch.setenv("MY_KEY", "secret")
        tool = {"secrets": {"env": {"key": "MY_KEY"}}}
        result = resolve_env_defaults(tool, {"other": "value"})
        assert result["other"] == "value"
        assert result["key"] == "secret"

    def test_empty_env_value_raises(self, monkeypatch):
        monkeypatch.setenv("EMPTY_VAR", "")
        tool = {"secrets": {"env": {"x": "EMPTY_VAR"}}}
        with pytest.raises(RuntimeError):
            resolve_env_defaults(tool, {})


# ---------------------------------------------------------------------------
# build_runtime_context
# ---------------------------------------------------------------------------

class TestBuildRuntimeContext:
    def test_basic_structure(self):
        tool = {"name": "my_tool", "description": "does stuff", "auth": {"role": "admin"}}
        ctx = MagicMock()
        result = build_runtime_context(tool, ctx)
        assert result["tool_name"] == "my_tool"
        assert result["tool_description"] == "does stuff"
        assert result["auth"] == {"role": "admin"}
        assert result["mcp_context"] is ctx

    def test_auth_defaults_to_empty_dict(self):
        result = build_runtime_context({"name": "t"}, None)
        assert result["auth"] == {}

    def test_mcp_context_can_be_none(self):
        result = build_runtime_context({"name": "t"}, None)
        assert result["mcp_context"] is None

    def test_description_defaults_to_empty(self):
        result = build_runtime_context({"name": "t"}, None)
        assert result["tool_description"] == ""


# ---------------------------------------------------------------------------
# _build_typed_signature
# ---------------------------------------------------------------------------

class TestBuildTypedSignature:
    def _schema(self, props, required=None):
        return {"input_schema": {"type": "object", "properties": props, "required": required or []}}

    def test_ctx_always_first(self):
        sig, _ = _build_typed_signature(self._schema({}))
        assert list(sig.parameters.keys())[0] == "ctx"

    def test_required_field_has_no_default(self):
        sig, _ = _build_typed_signature(self._schema({"q": {"type": "string"}}, required=["q"]))
        assert sig.parameters["q"].default is inspect.Parameter.empty

    def test_optional_field_defaults_to_none(self):
        sig, _ = _build_typed_signature(self._schema({"q": {"type": "string"}}))
        assert sig.parameters["q"].default is None

    def test_type_mapping(self):
        props = {
            "s": {"type": "string"},
            "i": {"type": "integer"},
            "f": {"type": "number"},
            "b": {"type": "boolean"},
            "o": {"type": "object"},
            "a": {"type": "array"},
        }
        sig, annotations = _build_typed_signature(self._schema(props, required=list(props.keys())))
        assert annotations["s"] is str
        assert annotations["i"] is int
        assert annotations["f"] is float
        assert annotations["b"] is bool
        assert annotations["o"] is dict
        assert annotations["a"] is list

    def test_default_from_schema(self):
        props = {"greeting": {"type": "string", "default": "hello"}}
        sig, _ = _build_typed_signature(self._schema(props))
        assert sig.parameters["greeting"].default == "hello"


# ---------------------------------------------------------------------------
# register_tool (integration-ish: verifies the dynamic closure behaviour)
# ---------------------------------------------------------------------------

class TestRegisterTool:
    def test_handler_called_with_context_and_kwargs(self):
        handler = AsyncMock(return_value={"ok": True})
        tool_spec = {
            "name": "test_tool",
            "description": "A test",
            "input_schema": {
                "type": "object",
                "properties": {"msg": {"type": "string"}},
                "required": ["msg"],
            },
        }
        # We patch mcp.tool to avoid touching the real FastMCP registry
        with patch("server.mcp") as mock_mcp:
            mock_mcp.tool.return_value = lambda fn: fn
            register_tool(tool_spec, handler)

    @pytest.mark.asyncio
    async def test_dynamic_tool_calls_handler(self):
        """Verify that the closure built by register_tool calls the handler correctly."""
        calls = []

        async def my_handler(context, msg):
            calls.append((context, msg))
            return {"ok": True}

        tool_spec = {
            "name": "test_tool",
            "description": "test",
            "input_schema": {
                "type": "object",
                "properties": {"msg": {"type": "string"}},
                "required": ["msg"],
            },
        }

        captured_fn = None

        def fake_tool_decorator(**kwargs):
            def decorator(fn):
                nonlocal captured_fn
                captured_fn = fn
                return fn
            return decorator

        with patch("server.mcp") as mock_mcp:
            mock_mcp.tool.side_effect = fake_tool_decorator
            register_tool(tool_spec, my_handler)

        assert captured_fn is not None
        ctx = MagicMock()
        result = await captured_fn(ctx, msg="hello")
        assert result == {"ok": True}
        assert calls[0][1] == "hello"

    @pytest.mark.asyncio
    async def test_handler_exception_returns_error_dict(self):
        async def bad_handler(context, **kwargs):
            raise ValueError("something broke")

        tool_spec = {
            "name": "bad_tool",
            "description": "breaks",
            "input_schema": {"type": "object", "properties": {}, "required": []},
        }

        captured_fn = None

        def fake_tool_decorator(**kwargs):
            def decorator(fn):
                nonlocal captured_fn
                captured_fn = fn
                return fn
            return decorator

        with patch("server.mcp") as mock_mcp:
            mock_mcp.tool.side_effect = fake_tool_decorator
            register_tool(tool_spec, bad_handler)

        result = await captured_fn(MagicMock())
        assert result["ok"] is False
        assert "something broke" in result["error"]
        assert result["tool"] == "bad_tool"

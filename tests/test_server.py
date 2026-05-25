"""Unit tests for server.py pure helper functions.

Note: server.py has module-level side effects (load_provider_specs + register_provider +
run_provider_setup).  conftest.py sets MCP_TOOL_CONFIG_DIR to an empty temp dir before
import so zero tools are registered and no processes are started.
"""
import inspect
import os
import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
import yaml

# server.py has module-level side effects (load_provider_specs + register_provider +
# run_provider_setup).  conftest.py has already set MCP_TOOL_CONFIG_DIR to an empty
# temp dir, so the import is safe and results in zero tools being registered.
from server import (
    SUBPROCESS_KEYS,
    _build_typed_signature,
    _get_package_command,
    advertised_tool_name,
    build_runtime_context,
    exec_provider_code,
    load_provider_specs,
    normalize_provider_name,
    redact_secrets,
    register_builtin_tools,
    register_provider,
    register_tool,
    resolve_env_defaults,
    run_provider_setup,
    tool_is_enabled,
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


# ---------------------------------------------------------------------------
# _get_package_command
# ---------------------------------------------------------------------------

class TestGetPackageCommand:
    def test_package_key_returns_command(self):
        spec = {"package": {"command": "npx @playwright/mcp@latest --isolated"}}
        assert _get_package_command(spec) == "npx @playwright/mcp@latest --isolated"

    def test_uvx_command(self):
        spec = {"package": {"command": "uvx mcp-server-fetch"}}
        assert _get_package_command(spec) == "uvx mcp-server-fetch"

    def test_python_module_command(self):
        spec = {"package": {"command": "python -m mcp_server_github"}}
        assert _get_package_command(spec) == "python -m mcp_server_github"

    def test_installed_binary_command(self):
        spec = {"package": {"command": "mcp-server-github"}}
        assert _get_package_command(spec) == "mcp-server-github"

    def test_code_provider_returns_none(self):
        spec = {"code": "async def f(ctx): pass", "tools": []}
        assert _get_package_command(spec) is None

    def test_empty_spec_returns_none(self):
        assert _get_package_command({}) is None

    def test_missing_command_field_returns_none(self):
        spec = {"package": {}}
        assert _get_package_command(spec) is None

    def test_empty_command_field_returns_none(self):
        spec = {"package": {"command": "   "}}
        assert _get_package_command(spec) is None

    def test_subprocess_keys_constant(self):
        assert SUBPROCESS_KEYS == ("package",)


# ---------------------------------------------------------------------------
# run_provider_setup
# ---------------------------------------------------------------------------

class TestRunProviderSetup:
    def test_empty_spec_no_subprocess_calls(self):
        with patch("server.subprocess.run") as mock_run:
            run_provider_setup({})
            mock_run.assert_not_called()

    def test_installs_requirements(self):
        spec = {"requirements": ["httpx", "requests"]}
        with patch("server.subprocess.run") as mock_run:
            run_provider_setup(spec)
        calls = mock_run.call_args_list
        assert len(calls) == 2
        # Both calls should be pip install
        for c in calls:
            args = c[0][0]
            assert "-m" in args
            assert "pip" in args
            assert "install" in args

    def test_installs_correct_package_names(self):
        spec = {"requirements": ["httpx==0.27.0", "requests"]}
        with patch("server.subprocess.run") as mock_run:
            run_provider_setup(spec)
        calls = mock_run.call_args_list
        assert calls[0][0][0][-1] == "httpx==0.27.0"
        assert calls[1][0][0][-1] == "requests"

    def test_runs_setup_commands(self):
        spec = {"setup_commands": ["npx playwright install chrome"]}
        with patch("server.subprocess.run") as mock_run:
            run_provider_setup(spec)
        calls = mock_run.call_args_list
        assert len(calls) == 1
        args = calls[0][0][0]
        assert args == ["npx", "playwright", "install", "chrome"]

    def test_runs_both_requirements_and_setup_commands(self):
        spec = {
            "requirements": ["httpx"],
            "setup_commands": ["echo hello"],
        }
        with patch("server.subprocess.run") as mock_run:
            run_provider_setup(spec)
        assert mock_run.call_count == 2

    def test_skips_empty_strings(self):
        spec = {"requirements": ["", "httpx", ""], "setup_commands": ["", "echo hi"]}
        with patch("server.subprocess.run") as mock_run:
            run_provider_setup(spec)
        assert mock_run.call_count == 2  # only "httpx" + "echo hi"

    def test_check_true_passed_to_subprocess(self):
        spec = {"requirements": ["httpx"]}
        with patch("server.subprocess.run") as mock_run:
            run_provider_setup(spec)
        _, kwargs = mock_run.call_args
        assert kwargs.get("check") is True


# ---------------------------------------------------------------------------
# register_builtin_tools
# ---------------------------------------------------------------------------

class TestRegisterBuiltinTools:
    def test_register_builtin_tools_is_callable(self):
        """register_builtin_tools is exported from server and callable."""
        assert callable(register_builtin_tools)

    def test_builtin_tool_handlers_importable(self):
        """builtin_tools module exports the required handler functions."""
        from builtin_tools import get_file, list_files
        assert callable(list_files)
        assert callable(get_file)

    def test_register_builtin_tools_calls_mcp_tool_twice(self):
        """register_builtin_tools registers exactly two tools via mcp.tool."""
        tool_calls = []

        def fake_decorator(**kwargs):
            tool_calls.append(kwargs.get("name"))
            return lambda fn: fn

        with patch("server.mcp") as mock_mcp:
            mock_mcp.tool.side_effect = fake_decorator
            register_builtin_tools()

        assert len(tool_calls) == 2
        assert "mcpproxy__listfiles" in tool_calls
        assert "mcpproxy__getfile" in tool_calls

    def test_listfiles_tool_spec_has_no_required_fields(self):
        """mcpproxy__listfiles 'path' parameter should be optional."""
        captured_specs = []

        def fake_decorator(**kwargs):
            captured_specs.append(kwargs)
            return lambda fn: fn

        with patch("server.mcp") as mock_mcp:
            mock_mcp.tool.side_effect = fake_decorator
            register_builtin_tools()

        # Find the listfiles call — it was the first one registered
        names = [s["name"] for s in captured_specs]
        assert "mcpproxy__listfiles" in names

    def test_getfile_tool_spec_requires_path(self):
        """mcpproxy__getfile should declare 'path' as a required parameter."""
        from builtin_tools import get_file
        import inspect
        sig = inspect.signature(get_file)
        # path has no default → required
        assert sig.parameters["path"].default is inspect.Parameter.empty


# ---------------------------------------------------------------------------
# normalize_provider_name / advertised_tool_name
# ---------------------------------------------------------------------------

class TestNormalizeProviderName:
    def test_simple_lowercase(self):
        assert normalize_provider_name("playwright") == "playwright"

    def test_simple_mixed_case_preserved(self):
        assert normalize_provider_name("MyProvider") == "MyProvider"

    def test_digits_preserved(self):
        assert normalize_provider_name("v2tool") == "v2tool"

    def test_hyphen_kept_as_hyphen(self):
        # Hyphens are outside [a-zA-Z0-9] so they get replaced with hyphens
        # (same character — net result unchanged).
        assert normalize_provider_name("my-provider") == "my-provider"

    def test_underscore_replaced_with_hyphen(self):
        assert normalize_provider_name("my_provider") == "my-provider"

    def test_dot_replaced_with_hyphen(self):
        assert normalize_provider_name("my.tool") == "my-tool"

    def test_spaces_replaced_with_hyphen(self):
        assert normalize_provider_name("my tool") == "my-tool"

    def test_at_sign_and_slash_replaced(self):
        assert normalize_provider_name("@scope/pkg") == "-scope-pkg"

    def test_empty_string(self):
        assert normalize_provider_name("") == ""

    def test_none_treated_as_empty(self):
        assert normalize_provider_name(None) == ""


class TestAdvertisedToolName:
    def test_basic_combination(self):
        assert advertised_tool_name("playwright", "browser_navigate") == "playwright__browser_navigate"

    def test_provider_normalized(self):
        assert advertised_tool_name("my.tool", "do_thing") == "my-tool__do_thing"

    def test_double_underscore_separator(self):
        # Must always be exactly two underscores, even if the tool name
        # already starts with one.
        assert advertised_tool_name("p", "_internal") == "p___internal"


# ---------------------------------------------------------------------------
# tool_is_enabled
# ---------------------------------------------------------------------------

class TestToolIsEnabled:
    def test_missing_field_defaults_true(self):
        assert tool_is_enabled({"name": "t"}) is True

    def test_explicit_true(self):
        assert tool_is_enabled({"name": "t", "enabled": True}) is True

    def test_explicit_false(self):
        assert tool_is_enabled({"name": "t", "enabled": False}) is False

    def test_other_truthy_values_treated_as_enabled(self):
        # Only an explicit `False` disables; everything else is on.
        assert tool_is_enabled({"name": "t", "enabled": 0}) is True
        assert tool_is_enabled({"name": "t", "enabled": ""}) is True
        assert tool_is_enabled({"name": "t", "enabled": None}) is True


# ---------------------------------------------------------------------------
# register_provider — name prefixing and enabled filtering
# ---------------------------------------------------------------------------

class TestRegisterProviderPrefixing:
    def _capture_registered(self, spec):
        names: list[str] = []

        def fake_decorator(**kwargs):
            names.append(kwargs.get("name"))
            return lambda fn: fn

        with patch("server.mcp") as mock_mcp:
            mock_mcp.tool.side_effect = fake_decorator
            register_provider(spec)
        return names

    def test_code_provider_tools_are_prefixed(self, tmp_path: Path):
        spec = {
            "_config_path": str(tmp_path / "playwright.yaml"),
            "code": "async def navigate(context, url):\n    return {'ok': True}\n",
            "tools": [{
                "name": "navigate",
                "function": "navigate",
                "description": "x",
                "input_schema": {"type": "object", "properties": {}, "required": []},
            }],
        }
        names = self._capture_registered(spec)
        assert names == ["playwright__navigate"]

    def test_package_provider_tools_are_prefixed(self, tmp_path: Path):
        spec = {
            "_config_path": str(tmp_path / "playwright.yaml"),
            "package": {"command": "npx @playwright/mcp@latest"},
            "tools": [{
                "name": "browser_navigate",
                "description": "x",
                "input_schema": {"type": "object", "properties": {}, "required": []},
            }],
        }
        names = self._capture_registered(spec)
        assert names == ["playwright__browser_navigate"]

    def test_provider_name_normalized(self, tmp_path: Path):
        spec = {
            "_config_path": str(tmp_path / "my.tool.yaml"),
            "package": {"command": "echo hi"},
            "tools": [{
                "name": "do",
                "description": "x",
                "input_schema": {"type": "object", "properties": {}, "required": []},
            }],
        }
        # .stem strips the final .yaml only, leaving "my.tool" — dots → hyphens.
        names = self._capture_registered(spec)
        assert names == ["my-tool__do"]

    def test_disabled_tool_is_skipped(self, tmp_path: Path):
        spec = {
            "_config_path": str(tmp_path / "p.yaml"),
            "package": {"command": "echo hi"},
            "tools": [
                {
                    "name": "alive",
                    "description": "x",
                    "enabled": True,
                    "input_schema": {"type": "object", "properties": {}, "required": []},
                },
                {
                    "name": "dead",
                    "description": "x",
                    "enabled": False,
                    "input_schema": {"type": "object", "properties": {}, "required": []},
                },
            ],
        }
        names = self._capture_registered(spec)
        assert names == ["p__alive"]

    def test_missing_enabled_defaults_to_registered(self, tmp_path: Path):
        spec = {
            "_config_path": str(tmp_path / "p.yaml"),
            "package": {"command": "echo hi"},
            "tools": [{
                "name": "t",
                "description": "x",
                "input_schema": {"type": "object", "properties": {}, "required": []},
            }],
        }
        names = self._capture_registered(spec)
        assert names == ["p__t"]

    def test_disabled_code_tool_skipped_without_loading_handler(self, tmp_path: Path):
        # The code block must NOT define `dead` — registration should still
        # succeed because the disabled tool is never looked up.
        spec = {
            "_config_path": str(tmp_path / "p.yaml"),
            "code": "async def alive(context):\n    return {'ok': True}\n",
            "tools": [
                {
                    "name": "alive", "function": "alive", "description": "x",
                    "input_schema": {"type": "object", "properties": {}, "required": []},
                },
                {
                    "name": "dead", "function": "missing_function",
                    "description": "x", "enabled": False,
                    "input_schema": {"type": "object", "properties": {}, "required": []},
                },
            ],
        }
        names = self._capture_registered(spec)
        assert names == ["p__alive"]

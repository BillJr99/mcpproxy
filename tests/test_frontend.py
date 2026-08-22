"""Unit tests for the HTTP frontend (frontend/app.py)."""
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import yaml
from fastapi.testclient import TestClient

from frontend.app import (
    _detect_package_manager,
    _provider_auth_info,
    _extract_functions,
    _extract_secret_env_keys,
    _parse_env_example,
    _provider_to_structured,
    _read_env_file,
    _structured_to_yaml,
    _validate_provider,
    _validate_rest,
    _write_env_file,
    _write_workdir_env_file,
    create_app,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def tools_dir(tmp_path: Path) -> Path:
    d = tmp_path / "tools"
    d.mkdir()
    return d


@pytest.fixture()
def env_path(tmp_path: Path) -> Path:
    return tmp_path / ".env"


@pytest.fixture()
def app(tools_dir, env_path):
    return create_app(config_dir=tools_dir, env_file=env_path)


@pytest.fixture()
def client(app):
    return TestClient(app)


# Minimal valid structured providers
CODE_PROVIDER = {
    "name": "myprovider",
    "type": "code",
    "documentation": "",
    "command": "",
    "code": "async def ping(context, msg='hi'):\n    return {'ok': True}\n",
    "requirements": [],
    "setup_commands": [],
    "tools": [{
        "name": "ping", "function": "ping", "description": "Ping tool",
        "documentation": "", "enabled": True, "parameters": [], "secrets": [],
    }],
}

PACKAGE_PROVIDER = {
    "name": "playwright",
    "type": "package",
    "documentation": "",
    "command": "npx @playwright/mcp@latest --isolated",
    "code": "",
    "requirements": [],
    "setup_commands": [],
    "tools": [{
        "name": "playwright_navigate", "function": "", "description": "Navigate",
        "documentation": "", "enabled": True, "parameters": [
            {"name": "url", "type": "string", "description": "URL", "required": True, "default": None}
        ], "secrets": [],
    }],
}

REPOSITORY_PROVIDER = {
    "name": "linkedin",
    "type": "repository",
    "documentation": "",
    "command": "node dist/main.js",
    "code": "",
    "requirements": [],
    "setup_commands": [],
    "repo_url": "https://github.com/felipfr/linkedin-mcpserver",
    "repo_ref": "main",
    "build_commands": ["npm install", "npm run build"],
    "workdir": "",
    "tools": [{
        "name": "search_jobs", "function": "", "description": "Search jobs",
        "documentation": "", "enabled": True,
        "parameters": [
            {"name": "query", "type": "string", "description": "Search query", "required": True, "default": None}
        ],
        "secrets": [],
    }],
}

REST_PROVIDER = {
    "name": "weather",
    "type": "rest",
    "documentation": "",
    "command": "",
    "code": "",
    "requirements": ["httpx"],
    "setup_commands": [],
    "rest": {
        "base_url": "https://api.example.com/v1",
        "headers": {"Accept": "application/json"},
        "auth": {
            "type": "authorization_code",
            "authorize_url": "https://auth.example.com/authorize",
            "token_url": "https://auth.example.com/token",
            "client_id_env": "WEATHER_CLIENT_ID",
            "client_secret_env": "WEATHER_CLIENT_SECRET",
            "scopes": ["read"],
        },
        "openapi": "",
        "endpoints": [
            {"name": "get_forecast", "method": "GET", "path": "/forecast/{city}",
             "path_params": ["city"], "query_params": ["units"], "body_params": []},
        ],
    },
    "tools": [{
        "name": "get_forecast", "function": "", "description": "Get the forecast",
        "documentation": "", "enabled": True,
        "parameters": [
            {"name": "city", "type": "string", "description": "City", "required": True, "default": None},
            {"name": "units", "type": "string", "description": "Units", "required": False, "default": None},
        ],
        "secrets": [],
    }],
}


# ---------------------------------------------------------------------------
# GET /api/tools
# ---------------------------------------------------------------------------

class TestListTools:
    def test_empty_dir(self, client):
        assert client.get("/api/tools").json() == []

    def test_lists_code_provider(self, app, tools_dir):
        content = _structured_to_yaml(CODE_PROVIDER)
        (tools_dir / "myprovider.yaml").write_text(content)
        r = TestClient(app).get("/api/tools")
        assert r.status_code == 200
        data = r.json()
        assert len(data) == 1
        assert data[0]["name"] == "myprovider"
        assert data[0]["is_package"] is False
        assert data[0]["provider_type"] == "code"

    def test_lists_package_provider(self, app, tools_dir):
        content = _structured_to_yaml(PACKAGE_PROVIDER)
        (tools_dir / "playwright.yaml").write_text(content)
        r = TestClient(app).get("/api/tools")
        data = r.json()
        assert data[0]["is_package"] is True
        assert data[0]["provider_type"] == "package"



# ---------------------------------------------------------------------------
# GET /api/tools/{name}
# ---------------------------------------------------------------------------

class TestGetTool:
    def test_existing(self, app, tools_dir):
        (tools_dir / "alpha.yaml").write_text(_structured_to_yaml(CODE_PROVIDER))
        r = TestClient(app).get("/api/tools/alpha")
        assert r.status_code == 200
        data = r.json()
        assert data["name"] == "alpha"
        assert data["type"] == "code"
        assert "tools" in data

    def test_not_found(self, client):
        assert client.get("/api/tools/nope").status_code == 404

    def test_package_provider(self, app, tools_dir):
        (tools_dir / "playwright.yaml").write_text(_structured_to_yaml(PACKAGE_PROVIDER))
        r = TestClient(app).get("/api/tools/playwright")
        data = r.json()
        assert data["type"] == "package"
        assert "command" in data
        assert data["command"] == "npx @playwright/mcp@latest --isolated"

    def test_requirements_and_setup_commands_returned(self, app, tools_dir):
        provider = {
            **CODE_PROVIDER,
            "requirements": ["httpx", "requests"],
            "setup_commands": ["echo hello"],
        }
        (tools_dir / "myprovider.yaml").write_text(_structured_to_yaml(provider))
        r = TestClient(app).get("/api/tools/myprovider")
        data = r.json()
        assert data["requirements"] == ["httpx", "requests"]
        assert data["setup_commands"] == ["echo hello"]


# ---------------------------------------------------------------------------
# POST /api/tools
# ---------------------------------------------------------------------------

class TestCreateTool:
    def test_create_code_provider(self, client):
        r = client.post("/api/tools", json={"name": "newprovider", "provider": CODE_PROVIDER})
        assert r.status_code == 200
        assert r.json()["ok"] is True

    def test_duplicate_409(self, app, tools_dir):
        (tools_dir / "dupe.yaml").write_text(_structured_to_yaml(CODE_PROVIDER))
        r = TestClient(app).post("/api/tools", json={"name": "dupe", "provider": CODE_PROVIDER})
        assert r.status_code == 409

    def test_missing_name_400(self, client):
        assert client.post("/api/tools", json={"provider": CODE_PROVIDER}).status_code == 400

    def test_invalid_name_400(self, client):
        r = client.post("/api/tools", json={"name": "../evil", "provider": CODE_PROVIDER})
        assert r.status_code == 400

    def test_no_tools_400(self, client):
        p = {**CODE_PROVIDER, "tools": []}
        r = client.post("/api/tools", json={"name": "x", "provider": p})
        assert r.status_code == 400

    def test_missing_code_400(self, client):
        p = {**CODE_PROVIDER, "code": ""}
        r = client.post("/api/tools", json={"name": "x", "provider": p})
        assert r.status_code == 400

    def test_create_package_provider(self, client):
        r = client.post("/api/tools", json={"name": "playwright", "provider": PACKAGE_PROVIDER})
        assert r.status_code == 200

    def test_package_yaml_uses_package_key(self, client, tools_dir):
        """Saved YAML must use 'package:' key, not 'npx:'."""
        client.post("/api/tools", json={"name": "pw", "provider": PACKAGE_PROVIDER})
        spec = yaml.safe_load((tools_dir / "pw.yaml").read_text())
        assert "package" in spec
        assert "npx" not in spec

    def test_remote_provider_saved_as_package(self, client, tools_dir):
        """The wizard's Remote MCP Server option produces a package provider
        whose YAML bridges the URL via mcp-remote (matching the Asana example)."""
        provider = {
            **PACKAGE_PROVIDER,
            "command": "npx -y mcp-remote https://mcp.asana.com/v2/mcp",
        }
        client.post("/api/tools", json={"name": "asana", "provider": provider})
        spec = yaml.safe_load((tools_dir / "asana.yaml").read_text())
        assert "package" in spec
        assert spec["package"]["command"] == "npx -y mcp-remote https://mcp.asana.com/v2/mcp"

    def test_requirements_saved_to_yaml(self, client, tools_dir):
        provider = {**CODE_PROVIDER, "requirements": ["httpx"]}
        client.post("/api/tools", json={"name": "myprovider", "provider": provider})
        spec = yaml.safe_load((tools_dir / "myprovider.yaml").read_text())
        assert spec.get("requirements") == ["httpx"]

    def test_setup_commands_saved_to_yaml(self, client, tools_dir):
        provider = {**PACKAGE_PROVIDER, "setup_commands": ["npx playwright install chrome"]}
        client.post("/api/tools", json={"name": "playwright", "provider": provider})
        spec = yaml.safe_load((tools_dir / "playwright.yaml").read_text())
        assert spec.get("setup_commands") == ["npx playwright install chrome"]

    def test_empty_requirements_not_written(self, client, tools_dir):
        """Empty requirements list should be omitted from YAML."""
        client.post("/api/tools", json={"name": "myprovider", "provider": CODE_PROVIDER})
        spec = yaml.safe_load((tools_dir / "myprovider.yaml").read_text())
        assert "requirements" not in spec or spec["requirements"] == []


# ---------------------------------------------------------------------------
# PUT /api/tools/{name}
# ---------------------------------------------------------------------------

class TestUpdateTool:
    def test_update_existing(self, app, tools_dir):
        (tools_dir / "p.yaml").write_text(_structured_to_yaml(CODE_PROVIDER))
        updated = {**CODE_PROVIDER, "documentation": "updated docs"}
        r = TestClient(app).put("/api/tools/p", json={"provider": updated})
        assert r.status_code == 200

    def test_creates_if_missing(self, client):
        r = client.put("/api/tools/brand_new", json={"provider": CODE_PROVIDER})
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# DELETE /api/tools/{name}
# ---------------------------------------------------------------------------

class TestDeleteTool:
    def test_delete(self, app, tools_dir):
        p = tools_dir / "todelete.yaml"
        p.write_text(_structured_to_yaml(CODE_PROVIDER))
        r = TestClient(app).delete("/api/tools/todelete")
        assert r.status_code == 200
        assert not p.exists()

    def test_delete_missing_404(self, client):
        assert client.delete("/api/tools/ghost").status_code == 404


# ---------------------------------------------------------------------------
# POST /api/validate
# ---------------------------------------------------------------------------

class TestValidate:
    def test_valid_code_provider(self, client):
        r = client.post("/api/validate", json={"provider": CODE_PROVIDER})
        assert r.json()["ok"] is True

    def test_valid_package_provider(self, client):
        r = client.post("/api/validate", json={"provider": PACKAGE_PROVIDER})
        assert r.json()["ok"] is True

    def test_missing_tools(self, client):
        r = client.post("/api/validate", json={"provider": {**CODE_PROVIDER, "tools": []}})
        assert not r.json()["ok"]

    def test_missing_code(self, client):
        r = client.post("/api/validate", json={"provider": {**CODE_PROVIDER, "code": ""}})
        assert not r.json()["ok"]

    def test_missing_command(self, client):
        r = client.post("/api/validate", json={"provider": {**PACKAGE_PROVIDER, "command": ""}})
        assert not r.json()["ok"]

    def test_tool_missing_description(self, client):
        p = {**CODE_PROVIDER, "tools": [{**CODE_PROVIDER["tools"][0], "description": ""}]}
        r = client.post("/api/validate", json={"provider": p})
        assert not r.json()["ok"]


# ---------------------------------------------------------------------------
# POST /api/extract-functions
# ---------------------------------------------------------------------------

class TestExtractFunctions:
    def test_finds_async_with_context(self, client):
        code = "async def my_fn(context, x: str) -> dict:\n    pass\n"
        r = client.post("/api/extract-functions", json={"code": code})
        fns = r.json()["functions"]
        assert len(fns) == 1
        assert fns[0]["name"] == "my_fn"

    def test_skips_fn_without_context(self, client):
        code = "async def no_ctx(x: str) -> dict:\n    pass\n"
        assert client.post("/api/extract-functions", json={"code": code}).json()["functions"] == []

    def test_syntax_error(self, client):
        r = client.post("/api/extract-functions", json={"code": "def broken(: pass"})
        assert not r.json()["ok"]


# ---------------------------------------------------------------------------
# POST /api/introspect
# ---------------------------------------------------------------------------

class TestIntrospect:
    def test_missing_command_400(self, client):
        r = client.post("/api/introspect", json={})
        assert r.status_code == 400

    def test_introspect_returns_tools(self, client):
        fake_tools = [{"name": "nav", "description": "Navigate", "inputSchema": {}}]
        with patch("process_runner.introspect", new=AsyncMock(return_value=fake_tools)):
            r = client.post("/api/introspect", json={"command": "npx @playwright/mcp@latest"})
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert len(data["tools"]) == 1
        assert data["tools"][0]["name"] == "nav"

    def test_introspect_detects_package_manager(self, client):
        with patch("process_runner.introspect", new=AsyncMock(return_value=[])):
            r = client.post("/api/introspect", json={"command": "uvx mcp-server-fetch"})
        assert r.json().get("package_manager") == "uvx"

    def test_introspect_error_returns_ok_false(self, client):
        with patch("process_runner.introspect", new=AsyncMock(side_effect=RuntimeError("failed"))):
            r = client.post("/api/introspect", json={"command": "bad-command"})
        assert r.status_code == 200
        assert r.json()["ok"] is False

    def test_requirements_installed_before_introspect(self, client):
        """pip install is called for each requirement before introspection."""
        with patch("process_runner.introspect", new=AsyncMock(return_value=[])), \
             patch("frontend.app.subprocess.run") as mock_run:
            r = client.post("/api/introspect", json={
                "command": "python -m mcp_server_github",
                "requirements": ["mcp-server-github"],
            })
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert "pip" in args
        assert "mcp-server-github" in args

    def test_old_introspect_npx_path_not_found(self, client):
        """The old /api/introspect-npx endpoint must no longer exist."""
        r = client.post("/api/introspect-npx", json={"command": "npx something"})
        assert r.status_code == 404

    def test_run_command_endpoint_not_found(self, client):
        """The /api/run-command endpoint has been removed."""
        r = client.post("/api/run-command", json={"command": "echo hi"})
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# GET & POST /api/env
# ---------------------------------------------------------------------------

class TestEnvEndpoints:
    def test_get_empty(self, client):
        r = client.get("/api/env")
        assert r.status_code == 200
        assert "vars" in r.json()

    def test_set_and_get(self, app, tools_dir, env_path):
        c = TestClient(create_app(config_dir=tools_dir, env_file=env_path))
        r = c.post("/api/env", json={"vars": {"MY_KEY": "abc123"}})
        assert r.status_code == 200
        assert env_path.exists()
        assert "MY_KEY=abc123" in env_path.read_text()

    def test_values_masked(self, tools_dir, env_path):
        env_path.write_text("SECRET_TOKEN=plaintext\n")
        c = TestClient(create_app(config_dir=tools_dir, env_file=env_path))
        assert c.get("/api/env").json()["vars"]["SECRET_TOKEN"] == "***"

    def test_invalid_key_400(self, client):
        assert client.post("/api/env", json={"vars": {"bad-key": "v"}}).status_code == 400

    def test_lowercase_key_400(self, client):
        assert client.post("/api/env", json={"vars": {"lowercase": "v"}}).status_code == 400


# ---------------------------------------------------------------------------
# GET /  (HTML)
# ---------------------------------------------------------------------------

class TestHTML:
    def test_returns_html(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        assert "mcpproxy" in r.text.lower()

    def test_no_raw_yaml_editor(self, client):
        r = client.get("/")
        assert "mode/yaml" not in r.text

    def test_no_discover_tab(self, client):
        # The legacy "Discover" tab and its modal must not appear in the UI.
        # (Substring "Discover" is allowed e.g. in "Discovered N env keys"
        # toast text introduced for repository providers.)
        text = client.get("/").text
        assert "id=\"discover-tab\"" not in text
        assert "Discover Tools" not in text

    def test_contains_api_calls(self, client):
        assert "/api/tools" in client.get("/").text

    def test_no_run_command_modal(self, client):
        """Run Command modal has been replaced by setup_commands field."""
        assert "cmd-modal" not in client.get("/").text

    def test_contains_introspect_endpoint(self, client):
        assert "/api/introspect" in client.get("/").text

    def test_contains_setup_commands_ui(self, client):
        assert "setup_commands" in client.get("/").text or "setup-commands" in client.get("/").text

    def test_wizard_has_package_type(self, client):
        assert "wzSelectType('package')" in client.get("/").text

    def test_wizard_has_two_type_cards(self, client):
        text = client.get("/").text
        assert "wzSelectType('code')" in text
        assert "wzSelectType('package')" in text

    def test_wizard_has_repository_card(self, client):
        text = client.get("/").text
        assert "wzSelectType('repository')" in text
        assert "wz-repo-url" in text
        assert "wz-repo-cmd" in text

    def test_clone_and_build_endpoint_exposed(self, client):
        assert "/api/clone-and-build" in client.get("/").text

    def test_editor_has_repository_box(self, client):
        text = client.get("/").text
        assert "repository-box" in text
        assert "f-repo-url" in text
        assert "build-commands-container" in text

    def test_wizard_defers_provider_creation_until_secrets(self, client):
        text = client.get("/").text
        # New idempotent helpers must be present
        assert "_wzRepoBuildAndIntrospect" in text
        assert "_wzRepoFinalize" in text
        assert "wzRepoCtx" in text

    def test_wizard_uses_put_for_idempotent_create(self, client):
        # _wzRepoFinalize must PUT to /api/tools/{name} so retries don't 409.
        text = client.get("/").text
        assert "PUT" in text
        assert "/api/tools/${ctx.name}" in text or "/api/tools/" in text

    def test_no_manual_introspect_button(self, client):
        """The 🔍 Introspect Tools button is replaced by auto-introspection."""
        text = client.get("/").text
        assert "wz-introspect-btn" not in text
        assert ">🔍 Introspect Tools<" not in text

    def test_no_manual_analyze_button(self, client):
        """The 🔍 Analyze Functions button is replaced by live auto-analysis."""
        assert ">🔍 Analyze Functions<" not in client.get("/").text

    def test_html_has_discover_functions(self, client):
        """Auto-discovery wiring is present in the JS."""
        assert "discoverFunctions" in client.get("/").text

    def test_html_has_enable_disable_helper(self, client):
        """Per-tool enable/disable hooks are wired up in the JS."""
        text = client.get("/").text
        assert "setToolEnabled" in text
        assert "knownFunctions" in text

    def test_html_has_function_picker(self, client):
        """The function-name dropdown ('Other…') is wired up."""
        text = client.get("/").text
        assert "onFnPick" in text
        assert "Other…" in text


# ---------------------------------------------------------------------------
# Pure function tests
# ---------------------------------------------------------------------------

class TestReadWriteEnvFile:
    def test_read_missing(self, tmp_path):
        assert _read_env_file(tmp_path / "no.env") == {}

    def test_read_basic(self, tmp_path):
        p = tmp_path / ".env"
        p.write_text("FOO=bar\nBAZ=qux\n")
        assert _read_env_file(p) == {"FOO": "bar", "BAZ": "qux"}

    def test_write_creates(self, tmp_path):
        p = tmp_path / ".env"
        _write_env_file(p, {"KEY": "val"})
        assert "KEY=val" in p.read_text()

    def test_write_updates(self, tmp_path):
        p = tmp_path / ".env"
        p.write_text("FOO=old\n")
        _write_env_file(p, {"FOO": "new"})
        assert "FOO=new" in p.read_text()
        assert "FOO=old" not in p.read_text()


class TestStructuredConversion:
    def test_code_round_trip(self):
        yaml_str = _structured_to_yaml(CODE_PROVIDER)
        spec = yaml.safe_load(yaml_str)
        assert "code" in spec
        assert not spec.get("package")
        assert not spec.get("npx")
        structured = _provider_to_structured("myprovider", spec)
        assert structured["type"] == "code"
        assert len(structured["tools"]) == 1

    def test_package_round_trip(self):
        yaml_str = _structured_to_yaml(PACKAGE_PROVIDER)
        spec = yaml.safe_load(yaml_str)
        assert "package" in spec
        assert spec["package"]["command"] == "npx @playwright/mcp@latest --isolated"
        structured = _provider_to_structured("playwright", spec)
        assert structured["type"] == "package"
        assert structured["command"] == "npx @playwright/mcp@latest --isolated"

    def test_package_yaml_uses_package_key_not_npx(self):
        yaml_str = _structured_to_yaml(PACKAGE_PROVIDER)
        spec = yaml.safe_load(yaml_str)
        assert "package" in spec
        assert "npx" not in spec

    def test_parameters_preserved(self):
        yaml_str = _structured_to_yaml(PACKAGE_PROVIDER)
        spec = yaml.safe_load(yaml_str)
        structured = _provider_to_structured("playwright", spec)
        params = structured["tools"][0]["parameters"]
        assert len(params) == 1
        assert params[0]["name"] == "url"
        assert params[0]["required"] is True

    def test_requirements_round_trip(self):
        provider = {**CODE_PROVIDER, "requirements": ["httpx", "requests"]}
        yaml_str = _structured_to_yaml(provider)
        spec = yaml.safe_load(yaml_str)
        assert spec["requirements"] == ["httpx", "requests"]
        structured = _provider_to_structured("p", spec)
        assert structured["requirements"] == ["httpx", "requests"]

    def test_setup_commands_round_trip(self):
        provider = {**PACKAGE_PROVIDER, "setup_commands": ["npx playwright install chrome"]}
        yaml_str = _structured_to_yaml(provider)
        spec = yaml.safe_load(yaml_str)
        assert spec["setup_commands"] == ["npx playwright install chrome"]
        structured = _provider_to_structured("p", spec)
        assert structured["setup_commands"] == ["npx playwright install chrome"]

    def test_enabled_true_round_trip(self):
        yaml_str = _structured_to_yaml(CODE_PROVIDER)
        spec = yaml.safe_load(yaml_str)
        assert spec["tools"][0]["enabled"] is True
        structured = _provider_to_structured("p", spec)
        assert structured["tools"][0]["enabled"] is True

    def test_enabled_false_round_trip(self):
        provider = {
            **CODE_PROVIDER,
            "tools": [{**CODE_PROVIDER["tools"][0], "enabled": False}],
        }
        yaml_str = _structured_to_yaml(provider)
        spec = yaml.safe_load(yaml_str)
        assert spec["tools"][0]["enabled"] is False
        structured = _provider_to_structured("p", spec)
        assert structured["tools"][0]["enabled"] is False

    def test_missing_enabled_in_yaml_defaults_true(self):
        """A YAML that pre-dates the `enabled` field is read as enabled=True."""
        spec = {
            "code": "async def t(context): pass\n",
            "tools": [{
                "name": "t", "function": "t", "description": "x",
                "input_schema": {"type": "object", "properties": {}, "required": []},
            }],
        }
        structured = _provider_to_structured("p", spec)
        assert structured["tools"][0]["enabled"] is True

    def test_enabled_always_written_explicitly(self):
        """Writer always emits enabled: true|false, never omits it."""
        yaml_str = _structured_to_yaml(CODE_PROVIDER)
        assert "enabled: true" in yaml_str
        provider = {
            **CODE_PROVIDER,
            "tools": [{**CODE_PROVIDER["tools"][0], "enabled": False}],
        }
        yaml_str = _structured_to_yaml(provider)
        assert "enabled: false" in yaml_str

    def test_empty_requirements_omitted_from_yaml(self):
        yaml_str = _structured_to_yaml(CODE_PROVIDER)  # requirements = []
        spec = yaml.safe_load(yaml_str)
        assert "requirements" not in spec

    def test_empty_setup_commands_omitted_from_yaml(self):
        yaml_str = _structured_to_yaml(CODE_PROVIDER)  # setup_commands = []
        spec = yaml.safe_load(yaml_str)
        assert "setup_commands" not in spec

    def test_requirements_defaults_to_empty_list(self):
        """Specs without requirements return [] not None."""
        spec = {"code": "pass\n", "tools": []}
        structured = _provider_to_structured("p", spec)
        assert structured["requirements"] == []

    def test_setup_commands_defaults_to_empty_list(self):
        spec = {"code": "pass\n", "tools": []}
        structured = _provider_to_structured("p", spec)
        assert structured["setup_commands"] == []


class TestExtractSecretEnvKeys:
    def test_empty(self):
        assert _extract_secret_env_keys({}) == []

    def test_finds_key(self):
        spec = {"tools": [{"secrets": {"env": {"key": "MY_KEY"}}}]}
        assert _extract_secret_env_keys(spec) == ["MY_KEY"]

    def test_deduplicates(self):
        spec = {"tools": [
            {"secrets": {"env": {"k": "SHARED"}}},
            {"secrets": {"env": {"k": "SHARED"}}},
        ]}
        assert _extract_secret_env_keys(spec).count("SHARED") == 1


class TestValidateProvider:
    def test_valid_code(self):
        assert _validate_provider(CODE_PROVIDER)["ok"] is True

    def test_valid_package(self):
        assert _validate_provider(PACKAGE_PROVIDER)["ok"] is True

    def test_no_tools(self):
        assert not _validate_provider({**CODE_PROVIDER, "tools": []})["ok"]

    def test_code_required_for_code_type(self):
        assert not _validate_provider({**CODE_PROVIDER, "code": ""})["ok"]

    def test_command_required_for_package_type(self):
        assert not _validate_provider({**PACKAGE_PROVIDER, "command": ""})["ok"]

    def test_requirements_must_be_list_if_present(self):
        p = {**CODE_PROVIDER, "requirements": "httpx"}  # string, not list
        assert not _validate_provider(p)["ok"]

    def test_setup_commands_must_be_list_if_present(self):
        p = {**CODE_PROVIDER, "setup_commands": "echo hi"}  # string, not list
        assert not _validate_provider(p)["ok"]

    def test_empty_requirements_list_is_valid(self):
        assert _validate_provider({**CODE_PROVIDER, "requirements": []})["ok"] is True

    def test_non_empty_requirements_list_is_valid(self):
        assert _validate_provider({**CODE_PROVIDER, "requirements": ["httpx"]})["ok"] is True


class TestExtractFunctionsPure:
    def test_basic(self):
        code = "async def my_fn(context, x: str) -> dict:\n    return {}\n"
        r = _extract_functions(code)
        assert r["ok"]
        assert r["functions"][0]["name"] == "my_fn"

    def test_syntax_error(self):
        r = _extract_functions("def broken(: pass")
        assert not r["ok"]


class TestDetectPackageManager:
    def test_npx(self):
        assert _detect_package_manager("npx @playwright/mcp@latest") == "npx"

    def test_uvx(self):
        assert _detect_package_manager("uvx mcp-server-fetch") == "uvx"

    def test_python(self):
        assert _detect_package_manager("python -m mcp_server_github") == "pip"

    def test_python3(self):
        assert _detect_package_manager("python3 -m something") == "pip"

    def test_npm(self):
        assert _detect_package_manager("npm run serve") == "npm"

    def test_installed_binary(self):
        assert _detect_package_manager("mcp-server-github") == "command"

    def test_empty_string(self):
        assert _detect_package_manager("") == "command"


# ---------------------------------------------------------------------------
# Repository provider — round-trip + validation
# ---------------------------------------------------------------------------

class TestRepositoryRoundTrip:
    def test_yaml_contains_both_blocks(self):
        yaml_str = _structured_to_yaml(REPOSITORY_PROVIDER)
        spec = yaml.safe_load(yaml_str)
        assert "package" in spec
        assert spec["package"]["command"] == "node dist/main.js"
        assert "repository" in spec
        assert spec["repository"]["url"] == "https://github.com/felipfr/linkedin-mcpserver"
        assert spec["repository"]["ref"] == "main"
        assert spec["repository"]["build_commands"] == ["npm install", "npm run build"]

    def test_round_trip_preserves_fields(self):
        yaml_str = _structured_to_yaml(REPOSITORY_PROVIDER)
        spec = yaml.safe_load(yaml_str)
        structured = _provider_to_structured("linkedin", spec)
        assert structured["type"] == "repository"
        assert structured["command"] == "node dist/main.js"
        assert structured["repo_url"] == "https://github.com/felipfr/linkedin-mcpserver"
        assert structured["repo_ref"] == "main"
        assert structured["build_commands"] == ["npm install", "npm run build"]
        # workdir is auto-derived from provider name when not explicitly set
        assert structured["workdir"].endswith("linkedin")

    def test_optional_fields_omitted_when_empty(self):
        provider = {**REPOSITORY_PROVIDER, "repo_ref": "", "build_commands": []}
        yaml_str = _structured_to_yaml(provider)
        spec = yaml.safe_load(yaml_str)
        assert "ref" not in spec["repository"]
        assert "build_commands" not in spec["repository"]

    def test_explicit_workdir_preserved(self):
        provider = {**REPOSITORY_PROVIDER, "workdir": "/custom/path"}
        yaml_str = _structured_to_yaml(provider)
        spec = yaml.safe_load(yaml_str)
        assert spec["repository"]["workdir"] == "/custom/path"
        structured = _provider_to_structured("linkedin", spec)
        assert structured["workdir"] == "/custom/path"


class TestRepositoryValidation:
    def test_valid_repository(self):
        assert _validate_provider(REPOSITORY_PROVIDER)["ok"] is True

    def test_missing_url(self):
        r = _validate_provider({**REPOSITORY_PROVIDER, "repo_url": ""})
        assert not r["ok"]
        assert any("repo_url" in e for e in r["errors"])

    def test_missing_command(self):
        r = _validate_provider({**REPOSITORY_PROVIDER, "command": ""})
        assert not r["ok"]
        assert any("command" in e for e in r["errors"])

    def test_build_commands_must_be_list(self):
        p = {**REPOSITORY_PROVIDER, "build_commands": "npm install"}  # string, not list
        assert not _validate_provider(p)["ok"]

    def test_empty_build_commands_is_valid(self):
        assert _validate_provider({**REPOSITORY_PROVIDER, "build_commands": []})["ok"] is True


class TestListToolsRepository:
    def test_repository_provider_listed(self, app, tools_dir):
        (tools_dir / "linkedin.yaml").write_text(_structured_to_yaml(REPOSITORY_PROVIDER))
        r = TestClient(app).get("/api/tools")
        data = r.json()
        assert data[0]["provider_type"] == "repository"
        assert data[0]["is_repository"] is True
        # is_package is also true because repository providers reuse the package: block
        assert data[0]["is_package"] is True


# ---------------------------------------------------------------------------
# /api/clone-and-build
# ---------------------------------------------------------------------------

class TestCloneAndBuild:
    def test_missing_name_400(self, client):
        r = client.post("/api/clone-and-build", json={"repo_url": "https://example.com/r.git"})
        assert r.status_code == 400

    def test_missing_url_400(self, client):
        r = client.post("/api/clone-and-build", json={"name": "myrepo"})
        assert r.status_code == 400

    def test_invalid_name_400(self, client):
        r = client.post("/api/clone-and-build", json={"name": "../evil", "repo_url": "https://e.com/r.git"})
        assert r.status_code == 400

    def test_clone_when_no_git_dir(self, client, tmp_path, monkeypatch):
        # Force the workdir to land inside tmp_path
        monkeypatch.setattr("frontend.app.REPOS_DIR", tmp_path)
        calls = []
        def fake_run(args, **kwargs):
            calls.append((list(args), kwargs.get("cwd")))
            class _R: returncode = 0
            return _R()
        with patch("frontend.app.subprocess.run", side_effect=fake_run):
            r = client.post("/api/clone-and-build", json={
                "name": "myrepo",
                "repo_url": "https://example.com/r.git",
                "build_commands": ["npm install", "npm run build"],
            })
        assert r.status_code == 200
        assert r.json()["ok"] is True
        # First call: git clone <url> <workdir>
        assert calls[0][0][:2] == ["git", "clone"]
        assert calls[0][0][2] == "https://example.com/r.git"
        # Build commands ran with cwd=workdir
        workdir = r.json()["workdir"]
        assert calls[1] == (["npm", "install"], workdir)
        assert calls[2] == (["npm", "run", "build"], workdir)

    def test_pull_when_git_dir_exists(self, client, tmp_path, monkeypatch):
        monkeypatch.setattr("frontend.app.REPOS_DIR", tmp_path)
        # Pre-create .git so the endpoint detects an existing clone
        wd = tmp_path / "myrepo"
        (wd / ".git").mkdir(parents=True)
        calls = []
        def fake_run(args, **kwargs):
            calls.append(list(args))
            class _R: returncode = 0
            return _R()
        with patch("frontend.app.subprocess.run", side_effect=fake_run):
            r = client.post("/api/clone-and-build", json={
                "name": "myrepo",
                "repo_url": "https://example.com/r.git",
            })
        assert r.json()["ok"] is True
        # First call should be a pull, not clone
        assert calls[0][:3] == ["git", "-C", str(wd)]
        assert calls[0][3] == "pull"

    def test_ref_checkout(self, client, tmp_path, monkeypatch):
        monkeypatch.setattr("frontend.app.REPOS_DIR", tmp_path)
        calls = []
        def fake_run(args, **kwargs):
            calls.append(list(args))
            class _R: returncode = 0
            return _R()
        with patch("frontend.app.subprocess.run", side_effect=fake_run):
            client.post("/api/clone-and-build", json={
                "name": "myrepo",
                "repo_url": "https://example.com/r.git",
                "ref": "v1.2.3",
            })
        # Expect clone + checkout
        assert any(c[:2] == ["git", "clone"] for c in calls)
        assert any("checkout" in c for c in calls)

    def test_build_failure_returns_ok_false(self, client, tmp_path, monkeypatch):
        import subprocess as sp
        monkeypatch.setattr("frontend.app.REPOS_DIR", tmp_path)
        def fake_run(args, **kwargs):
            if "clone" in args:
                class _R: returncode = 0
                return _R()
            raise sp.CalledProcessError(1, args)
        with patch("frontend.app.subprocess.run", side_effect=fake_run):
            r = client.post("/api/clone-and-build", json={
                "name": "myrepo",
                "repo_url": "https://example.com/r.git",
                "build_commands": ["broken-command"],
            })
        assert r.json()["ok"] is False


# ---------------------------------------------------------------------------
# /api/introspect forwards cwd
# ---------------------------------------------------------------------------

class TestIntrospectCwd:
    def test_cwd_passed_through(self, client):
        captured = {}

        async def fake_introspect(command, cwd=None, env_keys=None):
            captured["command"] = command
            captured["cwd"] = cwd
            return []

        with patch("process_runner.introspect", new=fake_introspect):
            r = client.post("/api/introspect", json={
                "command": "node dist/main.js",
                "cwd": "/app/repos/linkedin",
            })
        assert r.status_code == 200
        assert captured["cwd"] == "/app/repos/linkedin"

    def test_no_cwd_when_omitted(self, client):
        captured = {}

        async def fake_introspect(command, cwd=None, env_keys=None):
            captured["cwd"] = cwd
            return []

        with patch("process_runner.introspect", new=fake_introspect):
            client.post("/api/introspect", json={"command": "echo hi"})
        assert captured["cwd"] is None


# ---------------------------------------------------------------------------
# .env.example parsing + workdir .env writing
# ---------------------------------------------------------------------------

class TestParseEnvExample:
    def test_returns_keys_in_order(self, tmp_path):
        (tmp_path / ".env.example").write_text(
            "# comment line\n"
            "\n"
            "FOO=bar\n"
            "BAZ=\n"
            'QUOTED="value with spaces"\n'
        )
        assert _parse_env_example(tmp_path) == ["FOO", "BAZ", "QUOTED"]

    def test_empty_when_no_file(self, tmp_path):
        assert _parse_env_example(tmp_path) == []

    def test_falls_back_to_env_sample(self, tmp_path):
        (tmp_path / ".env.sample").write_text("MY_KEY=x\n")
        assert _parse_env_example(tmp_path) == ["MY_KEY"]

    def test_falls_back_to_env_template(self, tmp_path):
        (tmp_path / ".env.template").write_text("TEMPLATE_KEY=x\n")
        assert _parse_env_example(tmp_path) == ["TEMPLATE_KEY"]

    def test_env_example_wins_over_sample(self, tmp_path):
        (tmp_path / ".env.example").write_text("A=1\n")
        (tmp_path / ".env.sample").write_text("B=2\n")
        assert _parse_env_example(tmp_path) == ["A"]


class TestWriteWorkdirEnvFile:
    def test_writes_only_set_keys(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MY_FOO", "fooval")
        monkeypatch.delenv("MY_BAR", raising=False)
        target = _write_workdir_env_file(tmp_path, ["MY_FOO", "MY_BAR"])
        text = target.read_text()
        assert "MY_FOO=fooval" in text
        assert "MY_BAR" not in text

    def test_creates_workdir_if_missing(self, tmp_path, monkeypatch):
        wd = tmp_path / "sub" / "wd"
        monkeypatch.setenv("X", "1")
        _write_workdir_env_file(wd, ["X"])
        assert (wd / ".env").exists()

    def test_empty_file_when_no_keys_set(self, tmp_path, monkeypatch):
        monkeypatch.delenv("UNSET_KEY", raising=False)
        target = _write_workdir_env_file(tmp_path, ["UNSET_KEY"])
        assert target.read_text() == ""


class TestExtractSecretEnvKeysIncludesRepo:
    def test_repo_env_keys_added(self):
        spec = {
            "tools": [{"secrets": {"env": {"a": "TOOL_KEY"}}}],
            "repository": {"env_keys": ["REPO_A", "REPO_B"]},
        }
        keys = _extract_secret_env_keys(spec)
        assert keys == ["TOOL_KEY", "REPO_A", "REPO_B"]

    def test_dedup_across_tool_and_repo(self):
        spec = {
            "tools": [{"secrets": {"env": {"a": "SHARED"}}}],
            "repository": {"env_keys": ["SHARED", "EXTRA"]},
        }
        keys = _extract_secret_env_keys(spec)
        assert keys == ["SHARED", "EXTRA"]


class TestRepositoryRoundTripEnvKeys:
    def test_round_trip_with_env_keys(self):
        provider = {**REPOSITORY_PROVIDER, "repo_env_keys": ["LINKEDIN_EMAIL", "LINKEDIN_PASSWORD"]}
        yaml_str = _structured_to_yaml(provider)
        spec = yaml.safe_load(yaml_str)
        assert spec["repository"]["env_keys"] == ["LINKEDIN_EMAIL", "LINKEDIN_PASSWORD"]
        structured = _provider_to_structured("linkedin", spec)
        assert structured["repo_env_keys"] == ["LINKEDIN_EMAIL", "LINKEDIN_PASSWORD"]

    def test_empty_env_keys_omitted_from_yaml(self):
        yaml_str = _structured_to_yaml(REPOSITORY_PROVIDER)  # repo_env_keys not set
        spec = yaml.safe_load(yaml_str)
        assert "env_keys" not in spec.get("repository", {})


class TestCloneAndBuildEnvKeys:
    def _patch_repos_dir(self, monkeypatch, tmp_path):
        monkeypatch.setattr("frontend.app.REPOS_DIR", tmp_path)

    def test_returns_env_keys_from_dot_env_example(self, client, tmp_path, monkeypatch):
        self._patch_repos_dir(monkeypatch, tmp_path)

        # Fake git clone: when called with "git clone <url> <wd>", write a
        # .env.example into <wd> as if the repo contained it.
        def fake_run(args, **kwargs):
            if len(args) >= 4 and args[0:2] == ["git", "clone"]:
                wd = Path(args[3])
                wd.mkdir(parents=True, exist_ok=True)
                (wd / ".env.example").write_text("API_KEY=\nUSERNAME=\n")
            class _R: returncode = 0
            return _R()

        with patch("frontend.app.subprocess.run", side_effect=fake_run):
            r = client.post("/api/clone-and-build", json={
                "name": "linkedin",
                "repo_url": "https://e.com/r.git",
                "build_commands": [],
            })
        assert r.json()["ok"] is True
        assert r.json()["env_keys"] == ["API_KEY", "USERNAME"]

    def test_returns_env_keys_even_when_build_fails(self, client, tmp_path, monkeypatch):
        import subprocess as sp
        self._patch_repos_dir(monkeypatch, tmp_path)

        def fake_run(args, **kwargs):
            if args[0:2] == ["git", "clone"]:
                wd = Path(args[3])
                wd.mkdir(parents=True, exist_ok=True)
                (wd / ".env.example").write_text("NEED_ME=\n")
                class _R: returncode = 0
                return _R()
            if "npm" in args:
                # build fails because the .env doesn't have the needed value
                raise sp.CalledProcessError(9, args)
            class _R: returncode = 0
            return _R()

        with patch("frontend.app.subprocess.run", side_effect=fake_run):
            r = client.post("/api/clone-and-build", json={
                "name": "linkedin",
                "repo_url": "https://e.com/r.git",
                "build_commands": ["npm install", "npm run build"],
            })
        data = r.json()
        assert data["ok"] is False
        # Crucially: env_keys still returned so the wizard can populate Secrets
        assert data["env_keys"] == ["NEED_ME"]
        assert data["failed_command"] == "npm install"

    def test_writes_env_file_before_build(self, client, tmp_path, monkeypatch):
        self._patch_repos_dir(monkeypatch, tmp_path)
        monkeypatch.setenv("NEED_ME", "supplied")

        def fake_run(args, **kwargs):
            if args[0:2] == ["git", "clone"]:
                wd = Path(args[3])
                wd.mkdir(parents=True, exist_ok=True)
                (wd / ".env.example").write_text("NEED_ME=\n")
            class _R: returncode = 0
            return _R()

        with patch("frontend.app.subprocess.run", side_effect=fake_run):
            r = client.post("/api/clone-and-build", json={
                "name": "linkedin",
                "repo_url": "https://e.com/r.git",
                "build_commands": ["npm install"],
            })
        wd = Path(r.json()["workdir"])
        env_file = wd / ".env"
        assert env_file.exists()
        assert "NEED_ME=supplied" in env_file.read_text()


class TestScanEnvExampleEndpoint:
    def test_returns_keys_for_existing_workdir(self, client, tmp_path):
        (tmp_path / ".env.example").write_text("A=\nB=\n")
        r = client.post("/api/scan-env-example", json={"workdir": str(tmp_path)})
        assert r.json()["ok"] is True
        assert r.json()["env_keys"] == ["A", "B"]

    def test_missing_workdir_400(self, client):
        r = client.post("/api/scan-env-example", json={})
        assert r.status_code == 400


class TestClientConfig:
    def test_web_terminal_enabled_by_default(self, client, monkeypatch):
        monkeypatch.delenv("MCPPROXY_WEB_TERMINAL", raising=False)
        body = client.get("/api/config").json()
        assert body["ok"] is True
        assert body["web_terminal"] is True

    def test_web_terminal_disabled(self, client, monkeypatch):
        monkeypatch.setenv("MCPPROXY_WEB_TERMINAL", "0")
        assert client.get("/api/config").json()["web_terminal"] is False


class TestWebTerminal:
    def test_runs_command_and_streams_output(self, client, monkeypatch):
        monkeypatch.setenv("MCPPROXY_WEB_TERMINAL", "1")
        chunks: list[bytes] = []
        with client.websocket_connect("/ws/terminal?cmd=echo+marker_7788") as ws:
            try:
                for _ in range(50):
                    chunks.append(ws.receive_bytes())
            except Exception:
                pass  # disconnect once the command exits and the PTY closes
        assert b"marker_7788" in b"".join(chunks)

    def test_disabled_gate_closes_with_message(self, client, monkeypatch):
        monkeypatch.setenv("MCPPROXY_WEB_TERMINAL", "0")
        with client.websocket_connect("/ws/terminal") as ws:
            msg = ws.receive_text()
        assert "disabled" in msg.lower()


# ---------------------------------------------------------------------------
# REST providers
# ---------------------------------------------------------------------------

class TestRestSpecConversion:
    def test_structured_to_yaml_emits_rest_block(self):
        out = _structured_to_yaml(REST_PROVIDER)
        spec = yaml.safe_load(out)
        assert spec["rest"]["base_url"] == "https://api.example.com/v1"
        assert spec["rest"]["auth"]["type"] == "authorization_code"
        assert spec["rest"]["endpoints"][0]["name"] == "get_forecast"
        assert "package" not in spec and "code" not in spec

    def test_rest_tool_has_no_function_field(self):
        spec = yaml.safe_load(_structured_to_yaml(REST_PROVIDER))
        assert "function" not in spec["tools"][0]

    def test_provider_to_structured_round_trips_rest(self):
        spec = yaml.safe_load(_structured_to_yaml(REST_PROVIDER))
        structured = _provider_to_structured("weather", spec)
        assert structured["type"] == "rest"
        assert structured["rest"]["base_url"] == "https://api.example.com/v1"
        assert structured["rest"]["auth"]["client_id_env"] == "WEATHER_CLIENT_ID"
        assert structured["rest"]["endpoints"][0]["path"] == "/forecast/{city}"

    def test_default_headers_and_query_api_key_round_trip(self, app, tools_dir):
        """Editor-set default headers and an api_key-in-query auth survive save."""
        provider = {
            **REST_PROVIDER,
            "rest": {
                **REST_PROVIDER["rest"],
                "headers": {"Accept": "application/json", "X-Trace": "1"},
                "auth": {"type": "api_key", "in": "query", "name": "apikey", "value_env": "DEMO_KEY"},
            },
        }
        r = TestClient(app).post("/api/tools", json={"name": "weather2", "provider": provider})
        assert r.status_code == 200, r.text
        spec = yaml.safe_load((tools_dir / "weather2.yaml").read_text())
        assert spec["rest"]["headers"] == {"Accept": "application/json", "X-Trace": "1"}
        assert spec["rest"]["auth"] == {"type": "api_key", "in": "query", "name": "apikey", "value_env": "DEMO_KEY"}
        # api_key value env surfaces as a secret key
        assert "DEMO_KEY" in r.json()["secret_keys"]
        # and it round-trips back into the structured editor form
        structured = _provider_to_structured("weather2", spec)
        assert structured["rest"]["headers"]["X-Trace"] == "1"
        assert structured["rest"]["auth"]["in"] == "query"

    def test_editor_update_preserves_edited_endpoints(self, app, tools_dir):
        """Simulate the inline editor saving a REST provider with an added
        endpoint + renamed tool — auth and endpoints must survive the PUT."""
        (tools_dir / "weather.yaml").write_text(_structured_to_yaml(REST_PROVIDER))
        edited = {**REST_PROVIDER}
        edited["rest"] = {
            **REST_PROVIDER["rest"],
            "base_url": "https://api.example.com/v2",
            "endpoints": REST_PROVIDER["rest"]["endpoints"] + [
                {"name": "list_alerts", "method": "GET", "path": "/alerts",
                 "path_params": [], "query_params": ["region"], "body_params": []},
            ],
        }
        edited["tools"] = REST_PROVIDER["tools"] + [
            {"name": "list_alerts", "function": "", "description": "List alerts",
             "documentation": "", "enabled": True, "parameters": [], "secrets": []},
        ]
        r = TestClient(app).put("/api/tools/weather", json={"provider": edited})
        assert r.status_code == 200
        spec = yaml.safe_load((tools_dir / "weather.yaml").read_text())
        assert spec["rest"]["base_url"] == "https://api.example.com/v2"
        names = {e["name"] for e in spec["rest"]["endpoints"]}
        assert names == {"get_forecast", "list_alerts"}
        assert spec["rest"]["auth"]["type"] == "authorization_code"


class TestValidateRest:
    def test_valid_rest_provider_ok(self):
        assert _validate_provider(REST_PROVIDER)["ok"] is True

    def test_missing_base_url_fails(self):
        bad = {**REST_PROVIDER, "rest": {**REST_PROVIDER["rest"], "base_url": ""}}
        result = _validate_provider(bad)
        assert result["ok"] is False
        assert any("base_url" in e for e in result["errors"])

    def test_client_credentials_requires_token_url(self):
        provider = {
            "type": "rest",
            "rest": {"base_url": "https://x", "auth": {"type": "client_credentials"},
                     "endpoints": [{"name": "t", "method": "GET", "path": "/"}]},
            "tools": [{"name": "t", "description": "d"}],
        }
        errors = _validate_rest(provider)
        assert any("token_url" in e for e in errors)
        assert any("client_id_env" in e for e in errors)

    def test_authorization_code_requires_authorize_url(self):
        provider = {
            "type": "rest",
            "rest": {"base_url": "https://x", "auth": {"type": "authorization_code"},
                     "endpoints": [{"name": "t", "method": "GET", "path": "/"}]},
            "tools": [{"name": "t", "description": "d"}],
        }
        errors = _validate_rest(provider)
        assert any("authorize_url" in e for e in errors)

    def test_requires_openapi_or_endpoints(self):
        provider = {
            "type": "rest",
            "rest": {"base_url": "https://x", "auth": {"type": "none"},
                     "openapi": "", "endpoints": []},
            "tools": [{"name": "t", "description": "d"}],
        }
        errors = _validate_rest(provider)
        assert any("openapi" in e or "endpoint" in e for e in errors)

    def test_unknown_auth_type_fails(self):
        provider = {
            "type": "rest",
            "rest": {"base_url": "https://x", "auth": {"type": "wat"},
                     "endpoints": [{"name": "t", "method": "GET", "path": "/"}]},
            "tools": [{"name": "t", "description": "d"}],
        }
        errors = _validate_rest(provider)
        assert any("auth.type" in e for e in errors)


def _bearer_provider(auth):
    """A minimal, otherwise-valid REST provider using bearer auth."""
    return {
        "type": "rest",
        "rest": {
            "base_url": "https://x",
            "auth": {"type": "bearer", **auth},
            "endpoints": [{"name": "t", "method": "GET", "path": "/"}],
        },
        "tools": [{"name": "t", "description": "d"}],
    }


class TestValidateBearerCredentialSource:
    """bearer auth takes exactly one of token_env / token_file (mirrors
    rest_provider._require_bearer_token, which has accepted both since the
    file-backed token commit)."""

    def test_token_env_alone_ok(self):
        assert _validate_rest(_bearer_provider({"token_env": "TOK"})) == []

    def test_token_file_alone_ok(self, tmp_path):
        token = tmp_path / "access_token"
        token.write_text("secret\n", encoding="utf-8")
        assert _validate_rest(_bearer_provider({"token_file": str(token)})) == []

    def test_neither_fails(self):
        errors = _validate_rest(_bearer_provider({}))
        assert any("exactly one" in e for e in errors)

    def test_both_fails(self, tmp_path):
        token = tmp_path / "access_token"
        token.write_text("secret\n", encoding="utf-8")
        errors = _validate_rest(
            _bearer_provider({"token_env": "TOK", "token_file": str(token)})
        )
        assert any("exactly one" in e for e in errors)

    def test_missing_token_file_reports_path(self, tmp_path):
        missing = tmp_path / "nope" / "access_token"
        errors = _validate_rest(_bearer_provider({"token_file": str(missing)}))
        assert any(str(missing) in e for e in errors)

    def test_empty_string_credentials_are_not_credentials(self):
        errors = _validate_rest(_bearer_provider({"token_env": "", "token_file": ""}))
        assert any("exactly one" in e for e in errors)


class TestBearerTokenFileRoundTrip:
    def test_token_file_survives_yaml_round_trip(self, tmp_path):
        token = tmp_path / "access_token"
        token.write_text("secret\n", encoding="utf-8")
        provider = {
            **REST_PROVIDER,
            "rest": {**REST_PROVIDER["rest"],
                     "auth": {"type": "bearer", "token_file": str(token)}},
        }
        spec = yaml.safe_load(_structured_to_yaml(provider))
        assert spec["rest"]["auth"]["token_file"] == str(token)
        assert "token_env" not in spec["rest"]["auth"]

    def test_blank_token_env_is_not_written(self, tmp_path):
        """The editor writes '' into cleared inputs — that must not reach YAML."""
        token = tmp_path / "access_token"
        token.write_text("secret\n", encoding="utf-8")
        provider = {
            **REST_PROVIDER,
            "rest": {**REST_PROVIDER["rest"],
                     "auth": {"type": "bearer", "token_env": "",
                              "token_file": str(token)}},
        }
        spec = yaml.safe_load(_structured_to_yaml(provider))
        assert "token_env" not in spec["rest"]["auth"]
        assert _validate_rest({"type": "rest", **spec, "tools": [{"name": "t"}]}) == []


class TestExtractSecretEnvKeysRest:
    def test_rest_auth_env_keys_extracted(self):
        spec = yaml.safe_load(_structured_to_yaml(REST_PROVIDER))
        keys = _extract_secret_env_keys(spec)
        assert "WEATHER_CLIENT_ID" in keys
        assert "WEATHER_CLIENT_SECRET" in keys


def _write_bearer_provider(tools_dir, token_path, name="asana-rest"):
    """Write a bearer/token_file provider YAML into the tools dir."""
    provider = {
        **REST_PROVIDER,
        "name": name,
        "rest": {**REST_PROVIDER["rest"],
                 "auth": {"type": "bearer", "token_file": str(token_path)}},
    }
    (tools_dir / f"{name}.yaml").write_text(_structured_to_yaml(provider))
    return provider


class TestProviderAuthInfo:
    def test_none_for_code_provider(self):
        spec = yaml.safe_load(_structured_to_yaml(CODE_PROVIDER))
        assert _provider_auth_info(spec) is None

    def test_none_for_rest_without_auth(self):
        provider = {**REST_PROVIDER,
                    "rest": {**REST_PROVIDER["rest"], "auth": {"type": "none"}}}
        spec = yaml.safe_load(_structured_to_yaml(provider))
        assert _provider_auth_info(spec) is None

    def test_reports_env_keys_for_oauth_rest(self):
        spec = yaml.safe_load(_structured_to_yaml(REST_PROVIDER))
        info = _provider_auth_info(spec)
        assert info["kind"] == "rest"
        assert info["renewable"] is True
        assert "WEATHER_CLIENT_ID" in info["env_keys"]

    def test_reports_unset_then_set_token_file(self, tmp_path, tools_dir):
        token = tmp_path / "secrets" / "access_token"
        provider = _write_bearer_provider(tools_dir, token)
        spec = yaml.safe_load(_structured_to_yaml(provider))

        info = _provider_auth_info(spec)
        assert info["token_file"] == str(token)
        assert info["token_file_set"] is False
        assert info["renewable"] is False      # nothing to refresh, but re-checkable
        assert info["refreshable"] is True

        token.parent.mkdir(parents=True)
        token.write_text("secret\n", encoding="utf-8")
        assert _provider_auth_info(spec)["token_file_set"] is True

    def test_whitespace_only_token_file_counts_as_unset(self, tmp_path, tools_dir):
        token = tmp_path / "access_token"
        token.write_text("\n", encoding="utf-8")
        provider = _write_bearer_provider(tools_dir, token)
        spec = yaml.safe_load(_structured_to_yaml(provider))
        assert _provider_auth_info(spec)["token_file_set"] is False

    def test_exposed_by_list_tools(self, app, tools_dir, tmp_path):
        _write_bearer_provider(tools_dir, tmp_path / "access_token")
        data = TestClient(app).get("/api/tools").json()
        assert data[0]["auth_info"]["type"] == "bearer"


class TestSecretFileEndpoint:
    def test_writes_declared_path_mode_600(self, app, tools_dir):
        token = tools_dir / "secrets" / "asana-rest" / "access_token"
        _write_bearer_provider(tools_dir, token)
        r = TestClient(app).post("/api/secret-file",
                                 json={"name": "asana-rest", "value": "  tok-123  "})
        assert r.status_code == 200 and r.json()["ok"] is True
        assert token.read_text(encoding="utf-8") == "tok-123\n"
        assert oct(token.stat().st_mode & 0o777) == "0o600"

    def test_overwrites_and_tightens_existing_file(self, app, tools_dir):
        token = tools_dir / "secrets" / "access_token"
        token.parent.mkdir(parents=True)
        token.write_text("old\n", encoding="utf-8")
        token.chmod(0o644)
        _write_bearer_provider(tools_dir, token)
        TestClient(app).post("/api/secret-file",
                             json={"name": "asana-rest", "value": "new"})
        assert token.read_text(encoding="utf-8") == "new\n"
        assert oct(token.stat().st_mode & 0o777) == "0o600"

    def test_rejects_empty_value(self, app, tools_dir):
        _write_bearer_provider(tools_dir, tools_dir / "secrets" / "access_token")
        r = TestClient(app).post("/api/secret-file",
                                 json={"name": "asana-rest", "value": "   "})
        assert r.status_code == 400

    def test_rejects_path_outside_the_mounted_roots(self, app, tools_dir, tmp_path):
        outside = tmp_path / "elsewhere" / "passwd"
        _write_bearer_provider(tools_dir, outside)
        r = TestClient(app).post("/api/secret-file",
                                 json={"name": "asana-rest", "value": "tok"})
        assert r.status_code == 400
        assert not outside.exists()

    def test_rejects_provider_without_token_file(self, app, tools_dir):
        (tools_dir / "weather.yaml").write_text(_structured_to_yaml(REST_PROVIDER))
        r = TestClient(app).post("/api/secret-file",
                                 json={"name": "weather", "value": "tok"})
        assert r.status_code == 400

    def test_unknown_provider_is_404(self, app):
        r = TestClient(app).post("/api/secret-file",
                                 json={"name": "nope", "value": "tok"})
        assert r.status_code == 404


class TestAuthRefreshEndpoint:
    def test_verifies_a_readable_token_file(self, app, tools_dir):
        token = tools_dir / "secrets" / "access_token"
        token.parent.mkdir(parents=True)
        token.write_text("tok\n", encoding="utf-8")
        _write_bearer_provider(tools_dir, token)
        body = TestClient(app).post("/api/auth-refresh", json={"name": "asana-rest"}).json()
        assert body["ok"] is True
        assert body["refreshed"] is False
        assert str(token) in body["message"]

    def test_reports_an_unreadable_token_file(self, app, tools_dir):
        _write_bearer_provider(tools_dir, tools_dir / "secrets" / "missing")
        body = TestClient(app).post("/api/auth-refresh", json={"name": "asana-rest"}).json()
        assert body["ok"] is False
        assert "error" in body

    def test_forces_a_refresh_for_oauth_rest(self, app, tools_dir):
        (tools_dir / "weather.yaml").write_text(_structured_to_yaml(REST_PROVIDER))
        with patch("rest_provider._AuthResolver.apply", new=AsyncMock()) as applied:
            body = TestClient(app).post("/api/auth-refresh", json={"name": "weather"}).json()
        assert body["ok"] is True and body["refreshed"] is True
        assert applied.await_args.kwargs["force_refresh"] is True

    def test_surfaces_the_authorize_url_when_consent_is_needed(self, app, tools_dir):
        from rest_provider import NeedsAuthorization
        (tools_dir / "weather.yaml").write_text(_structured_to_yaml(REST_PROVIDER))
        boom = AsyncMock(side_effect=NeedsAuthorization("weather", "https://auth/x"))
        with patch("rest_provider._AuthResolver.apply", new=boom):
            body = TestClient(app).post("/api/auth-refresh", json={"name": "weather"}).json()
        assert body["ok"] is False
        assert body["needs_authorization"] is True
        assert body["auth_url"] == "https://auth/x"

    def test_provider_without_auth_says_so(self, app, tools_dir):
        (tools_dir / "myprovider.yaml").write_text(_structured_to_yaml(CODE_PROVIDER))
        body = TestClient(app).post("/api/auth-refresh", json={"name": "myprovider"}).json()
        assert body["ok"] is False
        assert "no authentication" in body["error"]


class TestListToolsRest:
    def test_lists_rest_provider_is_rest_true(self, app, tools_dir):
        (tools_dir / "weather.yaml").write_text(_structured_to_yaml(REST_PROVIDER))
        data = TestClient(app).get("/api/tools").json()
        assert data[0]["is_rest"] is True
        assert data[0]["provider_type"] == "rest"


class TestIntrospectOpenAPIEndpoint:
    def test_returns_endpoints_and_tools(self, client):
        fake = (
            [{"name": "op", "method": "GET", "path": "/x",
              "path_params": [], "query_params": [], "body_params": []}],
            [{"name": "op", "description": "d", "input_schema": {"type": "object", "properties": {}, "required": []}}],
        )
        with patch("rest_provider.introspect_openapi", return_value=fake):
            r = client.post("/api/introspect-openapi", json={"openapi": "https://x/openapi.json"})
        body = r.json()
        assert body["ok"] is True
        assert body["endpoints"][0]["name"] == "op"
        assert body["tools"][0]["name"] == "op"

    def test_error_returns_ok_false(self, client):
        with patch("rest_provider.introspect_openapi", side_effect=RuntimeError("boom")):
            r = client.post("/api/introspect-openapi", json={"openapi": "https://x"})
        body = r.json()
        assert body["ok"] is False and "boom" in body["error"]

    def test_missing_source_is_400(self, client):
        r = client.post("/api/introspect-openapi", json={})
        assert r.status_code == 400

    def test_local_path_outside_files_dir_rejected(self, client, tmp_path, monkeypatch):
        import frontend.app as app_module
        monkeypatch.setattr(app_module, "FILES_DIR", tmp_path / "files")
        (tmp_path / "files").mkdir()
        # An absolute path outside the files dir (would otherwise be a file read).
        r = client.post("/api/introspect-openapi", json={"openapi": "/etc/hostname"})
        body = r.json()
        assert body["ok"] is False
        assert "files directory" in body["error"]

    def test_local_path_traversal_rejected(self, client, tmp_path, monkeypatch):
        import frontend.app as app_module
        files = tmp_path / "files"
        files.mkdir()
        monkeypatch.setattr(app_module, "FILES_DIR", files)
        (tmp_path / "secret.json").write_text("{}")
        r = client.post("/api/introspect-openapi", json={"openapi": "../secret.json"})
        assert r.json()["ok"] is False

    def test_local_path_inside_files_dir_allowed(self, client, tmp_path, monkeypatch):
        import frontend.app as app_module
        files = tmp_path / "files"
        files.mkdir()
        monkeypatch.setattr(app_module, "FILES_DIR", files)
        (files / "spec.json").write_text(json.dumps({
            "openapi": "3.0.0",
            "paths": {"/ping": {"get": {"operationId": "ping"}}},
        }))
        r = client.post("/api/introspect-openapi", json={"openapi": "spec.json"})
        body = r.json()
        assert body["ok"] is True
        assert body["endpoints"][0]["name"] == "ping"


class TestRestAuthorizeAndCallback:
    def test_rest_authorize_begins_flow(self, app, tools_dir, monkeypatch):
        monkeypatch.setenv("WEATHER_CLIENT_ID", "cid")
        (tools_dir / "weather.yaml").write_text(_structured_to_yaml(REST_PROVIDER))
        r = TestClient(app).post("/api/rest-authorize", json={"name": "weather"})
        body = r.json()
        assert body["ok"] is True
        assert body["auth_url"].startswith("https://auth.example.com/authorize?")
        assert "/oauth/callback" in body["redirect_uri"]

    def test_rest_authorize_rejects_non_auth_code(self, app, tools_dir):
        provider = {**REST_PROVIDER, "rest": {**REST_PROVIDER["rest"], "auth": {"type": "none"}}}
        (tools_dir / "weather.yaml").write_text(_structured_to_yaml(provider))
        r = TestClient(app).post("/api/rest-authorize", json={"name": "weather"})
        assert r.status_code == 400

    def test_callback_missing_code_is_400(self, client):
        r = client.get("/oauth/callback")
        assert r.status_code == 400

    def test_callback_escapes_error_param(self, client):
        r = client.get("/oauth/callback", params={"error": "<script>alert(1)</script>"})
        assert r.status_code == 400
        assert "<script>alert(1)</script>" not in r.text
        assert "&lt;script&gt;" in r.text

    def test_callback_completes_authorization(self, client):
        with patch("rest_provider.AuthCodeTokenStore.complete_authorization",
                   new=AsyncMock(return_value="tok")):
            r = client.get("/oauth/callback?code=c&state=s")
        assert r.status_code == 200
        assert "complete" in r.text.lower()


class TestRestWizardFlowIntegration:
    """Drive the exact backend API sequence the REST wizard JS performs:
    introspect OpenAPI → assemble provider → POST /api/tools → GET it back.

    Uses the real OpenAPI parser (not mocked), exercising the full path a user
    walks through the wizard, then asserts a valid, reloadable provider results.
    """

    OPENAPI = {
        "openapi": "3.0.0",
        "info": {"title": "Demo", "version": "1.0"},
        "paths": {
            "/users/{user_id}": {
                "get": {
                    "operationId": "get_user",
                    "summary": "Fetch a user",
                    "parameters": [
                        {"name": "user_id", "in": "path", "required": True, "schema": {"type": "string"}},
                        {"name": "expand", "in": "query", "schema": {"type": "string"}},
                    ],
                }
            },
            "/users": {
                "post": {
                    "operationId": "create_user",
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": {
                            "type": "object", "required": ["name"],
                            "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
                        }}},
                    },
                }
            },
        },
    }

    def test_full_wizard_sequence(self, app, tools_dir, tmp_path, monkeypatch):
        import frontend.app as app_module
        files = tmp_path / "files"
        files.mkdir()
        monkeypatch.setattr(app_module, "FILES_DIR", files)
        client = TestClient(app)
        spec_file = files / "openapi.json"
        spec_file.write_text(json.dumps(self.OPENAPI))

        # 1. Wizard step: introspect the OpenAPI spec (real parser, file in FILES_DIR).
        r = client.post("/api/introspect-openapi", json={"openapi": "openapi.json"})
        body = r.json()
        assert body["ok"] is True
        endpoints = body["endpoints"]
        tools_from_spec = {t["name"]: t for t in body["tools"]}
        assert {e["name"] for e in endpoints} == {"get_user", "create_user"}

        # 2. Wizard assembles the provider exactly like wzNext() does.
        provider = {
            "name": "demo", "type": "rest", "command": "", "code": "",
            "documentation": "", "requirements": ["httpx"], "setup_commands": [],
            "rest": {
                "base_url": "https://api.demo.test/v1", "headers": {},
                "auth": {
                    "type": "client_credentials",
                    "token_url": "https://auth.demo.test/token",
                    "client_id_env": "DEMO_ID", "client_secret_env": "DEMO_SECRET",
                    "scopes": ["read"],
                },
                "openapi": "", "endpoints": endpoints,
            },
            "tools": [{
                "name": e["name"], "function": "",
                "description": tools_from_spec[e["name"]]["description"],
                "documentation": "", "enabled": True,
                "parameters": [
                    {"name": pn, "type": pdef.get("type", "string"),
                     "description": pdef.get("description", ""),
                     "required": pn in tools_from_spec[e["name"]]["input_schema"].get("required", []),
                     "default": None}
                    for pn, pdef in tools_from_spec[e["name"]]["input_schema"]["properties"].items()
                ],
                "secrets": [],
            } for e in endpoints],
        }

        # 3. Create it.
        r = client.post("/api/tools", json={"name": "demo", "provider": provider})
        assert r.status_code == 200, r.text

        # 4. Read it back as the editor would, and verify the on-disk YAML.
        got = client.get("/api/tools/demo").json()
        assert got["type"] == "rest"
        assert got["rest"]["base_url"] == "https://api.demo.test/v1"
        assert {e["name"] for e in got["rest"]["endpoints"]} == {"get_user", "create_user"}

        spec = yaml.safe_load((tools_dir / "demo.yaml").read_text())
        create = next(e for e in spec["rest"]["endpoints"] if e["name"] == "create_user")
        assert create["method"] == "POST"
        assert set(create["body_params"]) == {"name", "age"}
        # Secret env keys surface for the wizard's Secrets step.
        assert set(r.json()["secret_keys"]) >= {"DEMO_ID", "DEMO_SECRET"}


# ---------------------------------------------------------------------------
# File manager endpoints
# ---------------------------------------------------------------------------

@pytest.fixture()
def file_roots(tmp_path: Path, tools_dir: Path) -> dict:
    files_dir = tmp_path / "files"
    repos_dir = tmp_path / "repos"
    files_dir.mkdir()
    repos_dir.mkdir()
    return {"tools": tools_dir, "files": files_dir, "repos": repos_dir}


@pytest.fixture()
def files_client(tools_dir, env_path, file_roots):
    app = create_app(config_dir=tools_dir, env_file=env_path, file_roots=file_roots)
    return TestClient(app)


class TestFilesAPI:
    def test_list_empty_root(self, files_client):
        r = files_client.get("/api/files", params={"root": "tools"})
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["entries"] == []
        assert set(data["roots"]) == {"tools", "files", "repos"}

    def test_list_entries_shape_and_order(self, files_client, file_roots):
        root = file_roots["tools"]
        (root / "zfile.txt").write_text("hello")
        (root / "adir").mkdir()
        r = files_client.get("/api/files", params={"root": "tools"})
        entries = r.json()["entries"]
        # Directories sort first.
        assert [e["name"] for e in entries] == ["adir", "zfile.txt"]
        f = entries[1]
        assert f["type"] == "file" and f["size"] == 5 and f["path"] == "zfile.txt"
        assert entries[0]["type"] == "directory"

    def test_mkdir_nested_and_list_into(self, files_client, file_roots):
        r = files_client.post("/api/files/mkdir", json={"root": "tools", "path": "secrets/inner"})
        assert r.status_code == 200, r.text
        assert (file_roots["tools"] / "secrets" / "inner").is_dir()
        r = files_client.get("/api/files", params={"root": "tools", "path": "secrets"})
        assert [e["name"] for e in r.json()["entries"]] == ["inner"]

    def test_mkdir_empty_path_rejected(self, files_client):
        r = files_client.post("/api/files/mkdir", json={"root": "tools", "path": ""})
        assert r.status_code == 400

    def test_upload_download_delete_roundtrip(self, files_client, file_roots):
        files_client.post("/api/files/mkdir", json={"root": "tools", "path": "secrets"})
        r = files_client.post(
            "/api/files/upload",
            data={"root": "tools", "path": "secrets"},
            files={"file": ("client_secret.json", b'{"installed": {}}', "application/json")},
        )
        assert r.status_code == 200, r.text
        assert r.json()["path"] == "secrets/client_secret.json"
        on_disk = file_roots["tools"] / "secrets" / "client_secret.json"
        assert on_disk.read_bytes() == b'{"installed": {}}'

        r = files_client.get(
            "/api/files/download", params={"root": "tools", "path": "secrets/client_secret.json"}
        )
        assert r.status_code == 200
        assert r.content == b'{"installed": {}}'
        assert "client_secret.json" in r.headers["content-disposition"]

        r = files_client.delete(
            "/api/files", params={"root": "tools", "path": "secrets/client_secret.json"}
        )
        assert r.status_code == 200
        assert not on_disk.exists()

    def test_upload_creates_target_dir(self, files_client, file_roots):
        r = files_client.post(
            "/api/files/upload",
            data={"root": "files", "path": "new/sub"},
            files={"file": ("a.txt", b"x", "text/plain")},
        )
        assert r.status_code == 200, r.text
        assert (file_roots["files"] / "new" / "sub" / "a.txt").exists()

    def test_upload_filename_sanitized_to_basename(self, files_client, file_roots):
        r = files_client.post(
            "/api/files/upload",
            data={"root": "tools", "path": ""},
            files={"file": ("../evil.txt", b"x", "text/plain")},
        )
        assert r.status_code == 200, r.text
        assert (file_roots["tools"] / "evil.txt").exists()
        assert not (file_roots["tools"].parent / "evil.txt").exists()

    def test_upload_size_limit(self, files_client, file_roots, monkeypatch):
        import frontend.app as app_module
        monkeypatch.setattr(app_module, "MAX_UPLOAD_BYTES", 10)
        r = files_client.post(
            "/api/files/upload",
            data={"root": "tools", "path": ""},
            files={"file": ("big.bin", b"x" * 100, "application/octet-stream")},
        )
        assert r.status_code == 413
        assert not (file_roots["tools"] / "big.bin").exists()

    def test_download_directory_rejected(self, files_client, file_roots):
        (file_roots["tools"] / "adir").mkdir()
        r = files_client.get("/api/files/download", params={"root": "tools", "path": "adir"})
        assert r.status_code == 404

    def test_delete_root_rejected(self, files_client):
        for path in ("", "/", "."):
            r = files_client.delete("/api/files", params={"root": "tools", "path": path})
            assert r.status_code == 400, path

    def test_delete_nonempty_dir_requires_recursive(self, files_client, file_roots):
        d = file_roots["tools"] / "full"
        d.mkdir()
        (d / "x.txt").write_text("x")
        r = files_client.delete("/api/files", params={"root": "tools", "path": "full"})
        assert r.status_code == 400
        r = files_client.delete(
            "/api/files", params={"root": "tools", "path": "full", "recursive": "true"}
        )
        assert r.status_code == 200
        assert not d.exists()

    def test_absolute_path_treated_as_root_relative(self, files_client):
        # Leading slashes are stripped: "/etc/passwd" means "<root>/etc/passwd",
        # which does not exist — the real /etc/passwd is never touched.
        r = files_client.get("/api/files/download", params={"root": "tools", "path": "/etc/passwd"})
        assert r.status_code == 404

    def test_traversal_rejected(self, files_client):
        for path in ("../../etc", "a/../../etc"):
            for method, url, kwargs in (
                ("get", "/api/files", {"params": {"root": "tools", "path": path}}),
                ("post", "/api/files/mkdir", {"json": {"root": "tools", "path": path}}),
                ("get", "/api/files/download", {"params": {"root": "tools", "path": path}}),
                ("delete", "/api/files", {"params": {"root": "tools", "path": path}}),
            ):
                r = getattr(files_client, method)(url, **kwargs)
                assert r.status_code == 400, (method, url, path, r.text)

    def test_unknown_root_rejected(self, files_client):
        r = files_client.get("/api/files", params={"root": "bogus"})
        assert r.status_code == 400

    def test_symlink_escape_rejected_but_link_deletable(self, files_client, file_roots, tmp_path):
        outside = tmp_path / "outside.txt"
        outside.write_text("secret")
        link = file_roots["tools"] / "leak"
        link.symlink_to(outside)
        # Reading through the link is rejected (resolves outside the root)...
        r = files_client.get("/api/files/download", params={"root": "tools", "path": "leak"})
        assert r.status_code == 400
        # ...but it is listed as a symlink and the link itself can be removed.
        entries = files_client.get("/api/files", params={"root": "tools"}).json()["entries"]
        assert entries[0]["type"] == "symlink"
        r = files_client.delete("/api/files", params={"root": "tools", "path": "leak"})
        assert r.status_code == 200
        assert not link.is_symlink() and outside.exists()

    def test_default_roots_use_config_dir(self, client, tools_dir):
        # create_app without file_roots maps "tools" to the injected config_dir.
        (tools_dir / "p.yaml").write_text("tools: []\n")
        r = client.get("/api/files", params={"root": "tools"})
        assert r.status_code == 200
        assert [e["name"] for e in r.json()["entries"]] == ["p.yaml"]


class TestNewUISmoke:
    def test_index_contains_files_and_tooltester_ui(self, client):
        html = client.get("/").text
        for needle in ("files-modal", "tooltest-modal", "openFiles()", "openToolTester()",
                       "filesUpload", "ttInvoke"):
            assert needle in html, needle

    def test_index_contains_catalog_ui(self, client):
        html = client.get("/").text
        for needle in ("catalog-modal", "openCatalog()", "catalogConfigure", "best-for"):
            assert needle in html, needle


class TestCatalogAPI:
    def test_catalog_offline_returns_curated(self, client):
        data = client.get("/api/catalog").json()
        assert data["live"] is False
        assert data["errors"] == {}
        assert data["entries"], "curated catalog should not be empty"
        for e in data["entries"]:
            assert e["kind"] in ("mcp_remote", "rest_openapi")
            assert e["name"] and e["id"] and e["source"]

    def test_catalog_live_flag_without_probing_is_safe(self, client, monkeypatch):
        # Gate live probing off so the endpoint never touches the network even
        # when ?live=true is passed; it must still return the curated list.
        import catalog
        monkeypatch.setattr(catalog, "CATALOG_LIVE", False)
        data = client.get("/api/catalog?live=true").json()
        assert data["live"] is False
        assert data["entries"]


# ---------------------------------------------------------------------------
# OAuth bootstrap (provider-declared oauth: block)
# ---------------------------------------------------------------------------

def _oauth_provider_yaml(tools_dir: Path, tmp_path: Path, name: str = "gmailish") -> dict:
    cs = tmp_path / "client_secret.json"
    cs.write_text(json.dumps({"installed": {
        "client_id": "cid.apps.googleusercontent.com", "client_secret": "s",
    }}))
    spec = {
        "code": "async def ping(context):\n    return 1\n",
        "tools": [{"name": "ping", "function": "ping", "description": "Ping",
                   "input_schema": {"type": "object", "properties": {}, "required": []}}],
        "oauth": {
            "type": "google",
            "client_secret_file": str(cs),
            "token_file": str(tmp_path / "secrets" / "token.json"),
            "scopes": ["https://www.googleapis.com/auth/gmail.labels"],
        },
    }
    (tools_dir / f"{name}.yaml").write_text(yaml.safe_dump(spec))
    return spec


@pytest.fixture(autouse=True)
def _clear_oauth_flow_state():
    import rest_provider
    from rest_provider import AuthCodeTokenStore
    rest_provider.pending_rest_auth.clear()
    AuthCodeTokenStore._pending_flows.clear()
    yield
    rest_provider.pending_rest_auth.clear()
    AuthCodeTokenStore._pending_flows.clear()


class TestOauthBootstrap:
    def test_begin_flow_happy_path(self, client, tools_dir, tmp_path):
        _oauth_provider_yaml(tools_dir, tmp_path)
        r = client.post("/api/oauth-bootstrap", json={"name": "gmailish"})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["ok"] is True
        assert "access_type=offline" in data["auth_url"]
        assert "prompt=consent" in data["auth_url"]
        assert data["redirect_uri"].endswith("/oauth/callback")
        assert data["token"]["present"] is False
        # published to the banner channel
        import rest_provider
        assert "gmailish" in rest_provider.pending_rest_auth

    def test_unknown_provider_404(self, client):
        r = client.post("/api/oauth-bootstrap", json={"name": "nope"})
        assert r.status_code == 404

    def test_provider_without_oauth_block_400(self, client, tools_dir):
        (tools_dir / "plain.yaml").write_text(yaml.safe_dump({
            "code": "async def f(context):\n    return 1\n",
            "tools": [{"name": "f", "function": "f", "description": "x",
                       "input_schema": {"type": "object", "properties": {}}}],
        }))
        r = client.post("/api/oauth-bootstrap", json={"name": "plain"})
        assert r.status_code == 400

    def test_missing_client_secret_returns_error(self, client, tools_dir, tmp_path):
        spec = _oauth_provider_yaml(tools_dir, tmp_path)
        spec["oauth"]["client_secret_file"] = str(tmp_path / "missing.json")
        (tools_dir / "gmailish.yaml").write_text(yaml.safe_dump(spec))
        r = client.post("/api/oauth-bootstrap", json={"name": "gmailish"})
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is False
        assert "not found" in data["error"]

    def test_list_tools_exposes_token_state(self, client, tools_dir, tmp_path):
        spec = _oauth_provider_yaml(tools_dir, tmp_path)
        entry = next(p for p in client.get("/api/tools").json() if p["name"] == "gmailish")
        assert entry["oauth"]["type"] == "google"
        assert entry["oauth"]["has_refresh_token"] is False

        token_file = Path(spec["oauth"]["token_file"])
        token_file.parent.mkdir(parents=True)
        token_file.write_text(json.dumps({"refresh_token": "rt", "expiry": "2030-01-01T00:00:00Z"}))
        entry = next(p for p in client.get("/api/tools").json() if p["name"] == "gmailish")
        assert entry["oauth"]["has_refresh_token"] is True
        # providers without an oauth block expose null
        (tools_dir / "plain.yaml").write_text(yaml.safe_dump({"tools": []}))
        entry = next(p for p in client.get("/api/tools").json() if p["name"] == "plain")
        assert entry["oauth"] is None

    def test_structured_roundtrip_preserves_oauth(self, client, tools_dir, tmp_path):
        _oauth_provider_yaml(tools_dir, tmp_path)
        got = client.get("/api/tools/gmailish").json()
        assert got["oauth"]["type"] == "google"
        assert got["oauth"]["scopes"] == ["https://www.googleapis.com/auth/gmail.labels"]
        # write it back through the editor path and re-read
        r = client.put("/api/tools/gmailish", json={"provider": got})
        assert r.status_code == 200, r.text
        spec = yaml.safe_load((tools_dir / "gmailish.yaml").read_text())
        assert spec["oauth"]["type"] == "google"
        assert spec["oauth"]["client_secret_file"] == got["oauth"]["client_secret_file"]
        assert spec["oauth"]["token_file"] == got["oauth"]["token_file"]
        assert spec["oauth"]["scopes"] == got["oauth"]["scopes"]

    def test_validation_errors(self, tmp_path):
        from frontend.app import _validate_oauth
        cs = tmp_path / "cs.json"
        cs.write_text("{}")
        base = {"oauth": {"type": "google", "client_secret_file": str(cs),
                          "token_file": "/x/token.json", "scopes": ["a"]}}
        assert _validate_oauth(base) == []
        assert _validate_oauth({"oauth": {}}) == []  # no block → no errors

        errs = _validate_oauth({"oauth": {"type": "github"}})
        assert any("oauth.type" in e for e in errs)

        errs = _validate_oauth({"oauth": {"type": "google"}})
        assert any("client_secret_file" in e for e in errs)
        assert any("token_file" in e for e in errs)
        assert any("scopes" in e for e in errs)

        missing = dict(base["oauth"], client_secret_file=str(tmp_path / "nope.json"))
        errs = _validate_oauth({"oauth": missing})
        assert any("not found" in e for e in errs)

    def test_callback_completes_google_flow_and_writes_token(
        self, client, tools_dir, tmp_path, monkeypatch
    ):
        import oauth_bootstrap
        from rest_provider import AuthCodeTokenStore

        spec = _oauth_provider_yaml(tools_dir, tmp_path)
        r = client.post("/api/oauth-bootstrap", json={"name": "gmailish"})
        assert r.json()["ok"] is True
        state = next(iter(AuthCodeTokenStore._pending_flows))

        class _Resp:
            status_code = 200
            def json(self):
                return {"access_token": "at", "refresh_token": "rt", "expires_in": 3600}
            def raise_for_status(self):
                pass

        class _Client:
            def __init__(self, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *exc): return False
            async def post(self, url, data=None, **kw): return _Resp()

        monkeypatch.setattr(oauth_bootstrap.httpx, "AsyncClient", _Client)
        r = client.get(f"/oauth/callback?code=thecode&state={state}")
        assert r.status_code == 200
        assert "Authorization complete" in r.text
        record = json.loads(Path(spec["oauth"]["token_file"]).read_text())
        assert record["refresh_token"] == "rt"
        assert record["client_id"] == "cid.apps.googleusercontent.com"

    def test_html_contains_oauth_ui(self, client):
        html = client.get("/").text
        assert "oauth-bootstrap-btn" in html
        assert "/api/oauth-bootstrap" in html


# ---------------------------------------------------------------------------
# GET /api/provider-status
# ---------------------------------------------------------------------------

class TestProviderStatus:
    def test_empty_when_no_states_registered(self, client):
        import provider_status
        provider_status.clear()
        r = client.get("/api/provider-status")
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["providers"] == {}

    def test_pending_state(self, client):
        import provider_status
        provider_status.clear()
        provider_status.set_state(provider_status.ProviderState(name="myprov", status=provider_status.PENDING))
        r = client.get("/api/provider-status")
        assert r.status_code == 200
        entry = r.json()["providers"]["myprov"]
        assert entry["status"] == "pending"
        assert entry["error"] is None
        provider_status.clear()

    def test_ready_state(self, client):
        import provider_status
        provider_status.clear()
        provider_status.set_state(provider_status.ProviderState(name="myprov", status=provider_status.READY))
        r = client.get("/api/provider-status")
        entry = r.json()["providers"]["myprov"]
        assert entry["status"] == "ready"
        assert entry["error"] is None
        provider_status.clear()

    def test_failed_state_includes_error(self, client):
        import provider_status
        provider_status.clear()
        provider_status.set_state(provider_status.ProviderState(
            name="myprov", status=provider_status.FAILED, error="pip install returned exit code 1"
        ))
        r = client.get("/api/provider-status")
        entry = r.json()["providers"]["myprov"]
        assert entry["status"] == "failed"
        assert entry["error"] == "pip install returned exit code 1"
        provider_status.clear()

    def test_code_provider_has_no_auth_status(self, client, tools_dir):
        import provider_status

        (tools_dir / "local-code.yaml").write_text(
            yaml.safe_dump({"code": "def ping(): return 'pong'", "tools": []})
        )
        provider_status.clear()
        provider_status.set_state(
            provider_status.ProviderState(name="local-code", status=provider_status.READY)
        )
        try:
            entry = client.get("/api/provider-status").json()["providers"]["local-code"]
            assert entry["setup_status"] == "ready"
            assert entry["auth_status"] is None
        finally:
            provider_status.clear()

    def test_remote_provider_reports_authorization_required(self, client, tools_dir):
        import process_runner
        import provider_status

        command = "npx -y mcp-remote@0.1.38 https://mcp.asana.com/v2/mcp 8887"
        (tools_dir / "asana.yaml").write_text(
            yaml.safe_dump({"package": {"command": command}, "tools": []})
        )
        provider_status.clear()
        provider_status.set_state(
            provider_status.ProviderState(name="asana", status=provider_status.READY)
        )
        process_runner.pending_auth_urls[command] = "https://app.asana.com/oauth"
        process_runner.authenticated_commands.discard(command)
        try:
            entry = client.get("/api/provider-status").json()["providers"]["asana"]
            assert entry["setup_status"] == "ready"
            assert entry["auth_status"] == "authorization_required"
        finally:
            process_runner.pending_auth_urls.pop(command, None)
            provider_status.clear()

    def test_remote_provider_reports_successful_handshake(self, client, tools_dir):
        import process_runner
        import provider_status

        command = "npx -y mcp-remote@0.1.38 https://mcp.asana.com/v2/mcp 8887"
        (tools_dir / "asana.yaml").write_text(
            yaml.safe_dump({"package": {"command": command}, "tools": []})
        )
        provider_status.clear()
        provider_status.set_state(
            provider_status.ProviderState(name="asana", status=provider_status.READY)
        )
        process_runner.pending_auth_urls.pop(command, None)
        process_runner.authenticated_commands.add(command)
        try:
            entry = client.get("/api/provider-status").json()["providers"]["asana"]
            assert entry["setup_status"] == "ready"
            assert entry["auth_status"] == "authenticated"
        finally:
            process_runner.authenticated_commands.discard(command)
            provider_status.clear()

    def test_multiple_providers(self, client):
        import provider_status
        provider_status.clear()
        provider_status.set_state(provider_status.ProviderState(name="a", status=provider_status.READY))
        provider_status.set_state(provider_status.ProviderState(name="b", status=provider_status.PENDING))
        provider_status.set_state(provider_status.ProviderState(
            name="c", status=provider_status.FAILED, error="oops"
        ))
        data = client.get("/api/provider-status").json()
        assert data["providers"]["a"]["status"] == "ready"
        assert data["providers"]["b"]["status"] == "pending"
        assert data["providers"]["c"]["status"] == "failed"
        assert data["providers"]["c"]["error"] == "oops"
        provider_status.clear()


# ---------------------------------------------------------------------------
# UI smoke tests for new status + tools list features
# ---------------------------------------------------------------------------

class TestProviderStatusAndToolsListUI:
    def test_index_contains_tools_list_ui(self, client):
        html = client.get("/").text
        for needle in ("toolslist-modal", "openToolsList()", "tl-list", "tl-search", "tlRenderList"):
            assert needle in html, needle

    def test_index_contains_provider_status_polling(self, client):
        html = client.get("/").text
        assert "pollProviderStatus" in html
        assert "/api/provider-status" in html
        assert "updateStatusBadges" in html

    def test_index_contains_status_badge_classes(self, client):
        html = client.get("/").text
        assert "badge-status-pending" in html
        assert "badge-status-ready" in html
        assert "badge-status-failed" in html


# ---------------------------------------------------------------------------
# Manual OAuth callback (authorizing from a machine that can't reach this host)
# ---------------------------------------------------------------------------

ASANA_CMD = (
    "npx -y mcp-remote@0.1.38 https://mcp.asana.com/v2/mcp 8887 "
    "--static-oauth-client-info @/app/tools/secrets/asana/client_info.json "
    "--auth-timeout 600"
)


@pytest.fixture()
def pending_remote(tools_dir):
    """A configured mcp-remote provider that is waiting for authorization."""
    import process_runner

    (tools_dir / "asana.yaml").write_text(
        yaml.safe_dump({"package": {"command": ASANA_CMD}, "tools": []})
    )
    process_runner.pending_auth_urls[ASANA_CMD] = "https://app.asana.com/-/oauth"
    try:
        yield ASANA_CMD
    finally:
        process_runner.pending_auth_urls.pop(ASANA_CMD, None)
        process_runner.callback_listener_ports.pop(ASANA_CMD, None)
        process_runner.authenticated_commands.discard(ASANA_CMD)


class TestManualOAuthCallback:
    def test_unknown_target_is_rejected(self, client):
        r = client.post(
            "/api/oauth-manual-callback",
            json={"target": "nobody", "callback": "?code=abc&state=xyz"},
        )
        assert r.status_code == 404

    def test_a_target_that_is_not_pending_is_rejected(self, client, tools_dir):
        # Nothing is waiting, so there is no listener to replay into — refusing
        # here is what stops a replay reaching an unrelated process.
        (tools_dir / "idle.yaml").write_text(
            yaml.safe_dump({"package": {"command": ASANA_CMD}, "tools": []})
        )
        r = client.post(
            "/api/oauth-manual-callback",
            json={"target": "idle", "callback": "?code=abc&state=xyz"},
        )
        assert r.status_code == 404

    def test_malformed_callback_is_rejected_without_echoing_it(
        self, client, pending_remote
    ):
        r = client.post(
            "/api/oauth-manual-callback",
            json={"target": "asana", "callback": "SECRETNOISE"},
        )
        assert r.status_code == 400
        assert "SECRETNOISE" not in r.text

    def test_replays_to_the_bridge_by_provider_name(self, client, pending_remote):
        with patch(
            "oauth_callback_relay.deliver_to_bridge", new_callable=AsyncMock
        ) as deliver:
            deliver.return_value = 200
            r = client.post(
                "/api/oauth-manual-callback",
                json={
                    "target": "asana",
                    "callback": "http://localhost:8887/oauth/callback?code=abc&state=xyz",
                },
            )
        body = r.json()
        assert body["ok"] is True
        assert body["mode"] == "bridge-replay"
        assert body["port"] == 8887
        deliver.assert_awaited_once_with(
            8887, "/oauth/callback", {"code": "abc", "state": "xyz"}
        )

    def test_replays_to_the_bridge_by_spawn_command(self, client, pending_remote):
        with patch(
            "oauth_callback_relay.deliver_to_bridge", new_callable=AsyncMock
        ) as deliver:
            deliver.return_value = 200
            r = client.post(
                "/api/oauth-manual-callback",
                json={"target": pending_remote, "callback": "?code=abc&state=xyz"},
            )
        assert r.json()["ok"] is True
        deliver.assert_awaited_once()

    def test_uses_the_port_the_bridge_announced(self, client, pending_remote):
        import process_runner

        # mcp-remote chose its own port; that beats the one in the command.
        process_runner.callback_listener_ports[pending_remote] = 3334
        with patch(
            "oauth_callback_relay.deliver_to_bridge", new_callable=AsyncMock
        ) as deliver:
            deliver.return_value = 200
            r = client.post(
                "/api/oauth-manual-callback",
                json={"target": "asana", "callback": "?code=abc&state=xyz"},
            )
        assert r.json()["port"] == 3334
        assert deliver.await_args.args[0] == 3334

    def test_undeterminable_port_asks_for_the_missing_argument(
        self, client, tools_dir
    ):
        import process_runner

        command = "npx -y mcp-remote https://example.com/mcp"
        (tools_dir / "noport.yaml").write_text(
            yaml.safe_dump({"package": {"command": command}, "tools": []})
        )
        process_runner.pending_auth_urls[command] = "https://example.com/authorize"
        try:
            r = client.post(
                "/api/oauth-manual-callback",
                json={"target": "noport", "callback": "?code=abc&state=xyz"},
            )
        finally:
            process_runner.pending_auth_urls.pop(command, None)
        assert r.status_code == 409
        assert "second argument" in r.json()["detail"]

    def test_delivery_failure_is_reported_without_raising(self, client, pending_remote):
        import oauth_callback_relay as relay

        with patch(
            "oauth_callback_relay.deliver_to_bridge", new_callable=AsyncMock
        ) as deliver:
            deliver.side_effect = relay.CallbackDeliveryError("Nothing is listening")
            r = client.post(
                "/api/oauth-manual-callback",
                json={"target": "asana", "callback": "?code=abc&state=xyz"},
            )
        assert r.status_code == 200
        assert r.json() == {"ok": False, "error": "Nothing is listening"}

    def test_never_echoes_the_authorization_code(self, client, pending_remote):
        secret = "SECRET" + "CODE"
        with patch(
            "oauth_callback_relay.deliver_to_bridge", new_callable=AsyncMock
        ) as deliver:
            deliver.return_value = 200
            ok = client.post(
                "/api/oauth-manual-callback",
                json={"target": "asana", "callback": f"?code={secret}&state=xyz"},
            )
        assert secret not in ok.text
        with patch(
            "oauth_callback_relay.deliver_to_bridge", new_callable=AsyncMock
        ) as deliver:
            import oauth_callback_relay as relay

            deliver.side_effect = relay.CallbackDeliveryError("boom")
            bad = client.post(
                "/api/oauth-manual-callback",
                json={"target": "asana", "callback": f"?code={secret}&state=xyz"},
            )
        assert secret not in bad.text

    def test_in_process_flow_is_completed_from_the_state(self, client):
        # REST authorization_code and oauth: blocks keep their PKCE verifier
        # here, so a pasted code is exchanged directly instead of replayed.
        from rest_provider import AuthCodeTokenStore

        AuthCodeTokenStore._pending_flows["st8"] = {"kind": "rest", "created": 1e12}
        try:
            with patch.object(
                AuthCodeTokenStore,
                "complete_authorization",
                new_callable=AsyncMock,
            ) as complete:
                r = client.post(
                    "/api/oauth-manual-callback",
                    json={"callback": "?code=abc&state=st8"},
                )
            body = r.json()
            assert body["ok"] is True
            assert body["mode"] == "in-process"
            complete.assert_awaited_once_with("st8", "abc")
        finally:
            AuthCodeTokenStore._pending_flows.pop("st8", None)

    def test_in_process_exchange_failure_is_reported_generically(self, client):
        from rest_provider import AuthCodeTokenStore

        AuthCodeTokenStore._pending_flows["st9"] = {"kind": "rest", "created": 1e12}
        try:
            with patch.object(
                AuthCodeTokenStore, "complete_authorization", new_callable=AsyncMock
            ) as complete:
                # A provider/httpx error can quote the request URL, code included.
                complete.side_effect = RuntimeError("GET ...?code=LEAKED failed")
                r = client.post(
                    "/api/oauth-manual-callback",
                    json={"callback": "?code=LEAKED&state=st9"},
                )
        finally:
            AuthCodeTokenStore._pending_flows.pop("st9", None)
        assert r.json()["ok"] is False
        assert "LEAKED" not in r.text

    def test_unmatched_state_without_a_target_is_rejected(self, client):
        r = client.post(
            "/api/oauth-manual-callback", json={"callback": "?code=abc&state=unknown"}
        )
        assert r.status_code == 400


class TestOAuthReauthorize:
    def test_unknown_provider_is_rejected(self, client):
        assert client.post("/api/oauth-reauthorize", json={"target": "nope"}).status_code == 404

    def test_target_is_required(self, client):
        assert client.post("/api/oauth-reauthorize", json={}).status_code == 400

    def test_returns_the_existing_link_without_respawning(self, client, pending_remote):
        # A second bridge would collide on the callback port, so a flow that is
        # still waiting hands back the link it already printed.
        with patch("process_runner.introspect", new_callable=AsyncMock) as spawn:
            r = client.post("/api/oauth-reauthorize", json={"target": "asana"})
        spawn.assert_not_awaited()
        body = r.json()
        assert body["restarted"] is False
        assert body["auth_url"] == "https://app.asana.com/-/oauth"
        assert body["port"] == 8887

    def test_respawns_a_lapsed_flow_and_returns_the_new_link(self, client, tools_dir):
        import process_runner

        command = ASANA_CMD
        (tools_dir / "asana.yaml").write_text(
            yaml.safe_dump({"package": {"command": command}, "tools": []})
        )

        async def fake_introspect(cmd, **kwargs):
            # Stand in for the bridge printing a fresh URL to stderr.
            process_runner.pending_auth_urls[cmd] = "https://app.asana.com/-/oauth?new=1"
            process_runner.callback_listener_ports[cmd] = 8887
            await asyncio.sleep(30)

        try:
            with patch("process_runner.introspect", fake_introspect):
                r = client.post("/api/oauth-reauthorize", json={"target": "asana"})
            body = r.json()
            assert body["ok"] is True
            assert body["restarted"] is True
            assert body["auth_url"] == "https://app.asana.com/-/oauth?new=1"
            assert body["port"] == 8887
        finally:
            process_runner.pending_auth_urls.pop(command, None)
            process_runner.callback_listener_ports.pop(command, None)

    def test_reports_a_silent_success_when_the_cache_was_still_good(
        self, client, tools_dir
    ):
        (tools_dir / "asana.yaml").write_text(
            yaml.safe_dump({"package": {"command": ASANA_CMD}, "tools": []})
        )
        with patch("process_runner.introspect", new_callable=AsyncMock) as spawn:
            spawn.return_value = []
            r = client.post("/api/oauth-reauthorize", json={"target": "asana"})
        body = r.json()
        assert body["ok"] is True
        assert body["auth_url"] is None
        assert "cached token" in body["message"]


class TestOAuthCallbackStatus:
    def test_reports_listener_and_forwarder_state(
        self, client, pending_remote, monkeypatch
    ):
        monkeypatch.setenv("MCPPROXY_CALLBACK_FORWARD_PORTS", "8887")
        monkeypatch.setattr("oauth_callback_relay.probe_loopback_port", lambda p, **k: True)
        body = client.get("/api/oauth-callback-status").json()

        assert body["configured_forward_ports"] == [8887]
        entry = next(p for p in body["providers"] if p["provider"] == "asana")
        assert entry["port"] == 8887
        assert entry["port_source"] == "command"
        assert entry["listening"] is True
        assert entry["pending_authorization"] is True
        assert entry["authenticated"] is False
        # The summary keeps the bridge and URL but drops the credential-file flag.
        assert "client_info.json" not in entry["command_summary"]
        assert "https://mcp.asana.com/v2/mcp" in entry["command_summary"]

    def test_a_dead_listener_is_visible(self, client, pending_remote, monkeypatch):
        monkeypatch.setattr("oauth_callback_relay.probe_loopback_port", lambda p, **k: False)
        body = client.get("/api/oauth-callback-status").json()
        entry = next(p for p in body["providers"] if p["provider"] == "asana")
        # Pending but not listening: the bridge gave up and must be restarted.
        assert entry["pending_authorization"] is True
        assert entry["listening"] is False

    def test_unforwarded_port_is_flagged(self, client, pending_remote, monkeypatch):
        monkeypatch.setenv("MCPPROXY_CALLBACK_FORWARD_PORTS", "")
        monkeypatch.setattr("oauth_callback_relay.probe_loopback_port", lambda p, **k: True)
        body = client.get("/api/oauth-callback-status").json()
        port = next(p for p in body["ports"] if p["port"] == 8887)
        assert port["configured"] is False
        assert port["forwarded"] is False

    def test_bad_forward_port_configuration_is_surfaced(self, client, monkeypatch):
        monkeypatch.setenv("MCPPROXY_CALLBACK_FORWARD_PORTS", "not-a-port")
        body = client.get("/api/oauth-callback-status").json()
        assert body["configured_forward_ports"] == []
        assert "not-a-port" in body["forward_ports_error"]

    def test_non_remote_providers_are_not_listed(self, client, tools_dir):
        (tools_dir / "pw.yaml").write_text(
            yaml.safe_dump({"package": {"command": "npx @playwright/mcp"}, "tools": []})
        )
        body = client.get("/api/oauth-callback-status").json()
        assert [p["provider"] for p in body["providers"]] == []


class TestManualCallbackUI:
    def test_index_contains_the_manual_callback_modal(self, client):
        html = client.get("/").text
        for needle in (
            "mcb-modal",
            "openManualCallback(",
            "mcbSubmit",
            "mcbRestart",
            "mcb-target",
            "mcb-url",
            "mcb-diag",
            "/api/oauth-manual-callback",
            "/api/oauth-callback-status",
            "/api/oauth-reauthorize",
        ):
            assert needle in html, needle

    def test_every_authorize_surface_offers_manual_entry(self, client):
        html = client.get("/").text
        # banner, package-command box, and the shared link helper used by the
        # REST / oauth: / wizard status lines
        assert 'id="reauth-manual-btn"' in html
        assert "function mcbLink(" in html
        assert html.count("mcbLink(") >= 5


class TestBridgeErrorReporting:
    """A package provider's setup "succeeds" because building handlers only makes
    closures — so a bridge whose every spawn dies used to show as a clean, ready
    provider with the real cause only on the server's stdout."""

    @pytest.fixture()
    def broken_bridge(self, tools_dir):
        import process_runner
        import provider_status

        (tools_dir / "ghcopilot.yaml").write_text(
            yaml.safe_dump({"package": {"command": ASANA_CMD}, "tools": []})
        )
        provider_status.clear()
        provider_status.set_state(
            provider_status.ProviderState(name="ghcopilot", status=provider_status.READY)
        )
        process_runner.bridge_errors[ASANA_CMD] = "GITHUB_MCP_AUTH_HEADER is not set."
        try:
            yield
        finally:
            process_runner.bridge_errors.pop(ASANA_CMD, None)
            provider_status.clear()

    def test_provider_status_reports_the_bridge_error(self, client, broken_bridge):
        entry = client.get("/api/provider-status").json()["providers"]["ghcopilot"]
        # Setup and bridge health are separate axes on purpose.
        assert entry["setup_status"] == "ready"
        assert entry["error"] is None
        assert entry["bridge_error"] == "GITHUB_MCP_AUTH_HEADER is not set."

    def test_callback_status_reports_the_bridge_error(
        self, client, broken_bridge, monkeypatch
    ):
        monkeypatch.setattr("oauth_callback_relay.probe_loopback_port", lambda p, **k: False)
        body = client.get("/api/oauth-callback-status").json()
        entry = next(p for p in body["providers"] if p["provider"] == "ghcopilot")
        assert entry["bridge_error"] == "GITHUB_MCP_AUTH_HEADER is not set."

    def test_healthy_provider_reports_no_bridge_error(self, client, pending_remote):
        entry = client.get("/api/provider-status").json()["providers"]
        assert all(e["bridge_error"] is None for e in entry.values())


class TestStaleCallbackRejected:
    """A callback from an earlier attempt still parses, but the bridge has since
    generated a new PKCE verifier — delivering it burns the single-use code and
    fails with an opaque code_verifier mismatch."""

    @pytest.fixture()
    def bridge_with_state(self, tools_dir):
        import process_runner

        (tools_dir / "asana.yaml").write_text(
            yaml.safe_dump({"package": {"command": ASANA_CMD}, "tools": []})
        )
        process_runner.pending_auth_urls[ASANA_CMD] = (
            "https://app.asana.com/-/oauth_authorize?client_id=1&state=CURRENT"
        )
        try:
            yield
        finally:
            process_runner.pending_auth_urls.pop(ASANA_CMD, None)

    def test_mismatched_state_is_refused_without_delivering(
        self, client, bridge_with_state
    ):
        with patch(
            "oauth_callback_relay.deliver_to_bridge", new_callable=AsyncMock
        ) as deliver:
            r = client.post(
                "/api/oauth-manual-callback",
                json={"target": "asana", "callback": "?code=abc&state=STALE"},
            )
        assert r.status_code == 409
        assert "earlier authorization attempt" in r.json()["detail"]
        deliver.assert_not_awaited()

    def test_matching_state_is_delivered(self, client, bridge_with_state):
        with patch(
            "oauth_callback_relay.deliver_to_bridge", new_callable=AsyncMock
        ) as deliver:
            deliver.return_value = 200
            r = client.post(
                "/api/oauth-manual-callback",
                json={"target": "asana", "callback": "?code=abc&state=CURRENT"},
            )
        assert r.json()["ok"] is True
        deliver.assert_awaited_once()

    def test_no_state_on_either_side_still_delivers(self, client, pending_remote):
        # The pending URL here carries no state, so there is nothing to compare
        # against — refusing would block a legitimate paste.
        with patch(
            "oauth_callback_relay.deliver_to_bridge", new_callable=AsyncMock
        ) as deliver:
            deliver.return_value = 200
            r = client.post(
                "/api/oauth-manual-callback",
                json={"target": "asana", "callback": "?code=abc"},
            )
        assert r.json()["ok"] is True


class TestPackageEnvKeys:
    def test_package_env_keys_reach_the_secrets_ui(self):
        from frontend.app import _extract_secret_env_keys

        spec = {
            "package": {
                "command": "npx -y mcp-remote https://x/mcp --header Auth:${TOKEN}",
                "env_keys": ["TOKEN"],
            },
            "tools": [],
        }
        assert "TOKEN" in _extract_secret_env_keys(spec)


class TestBridgeBadgeUI:
    def test_index_renders_bridge_and_auth_badges(self, client):
        html = client.get("/").text
        for needle in ("bridge_error", "auth_status", "✗ bridge failed", "🔐 authorize"):
            assert needle in html, needle


class TestPackageEnvKeysRoundTrip:
    """env_keys names variables re-read from .env before each spawn. Dropping it
    on a save would silently break the provider the next time it starts."""

    SPEC = {
        "package": {
            "command": (
                "npx -y mcp-remote https://api.githubcopilot.com/mcp/ "
                "--header Authorization:${GITHUB_MCP_AUTH_HEADER}"
            ),
            "env_keys": ["GITHUB_MCP_AUTH_HEADER"],
        },
        "tools": [
            {
                "name": "get_me",
                "description": "Return the authorized user.",
                "input_schema": {"type": "object", "properties": {}},
            }
        ],
    }

    def test_survives_an_editor_round_trip(self):
        from frontend.app import _provider_to_structured, _structured_to_yaml

        structured = _provider_to_structured("ghcopilot", self.SPEC)
        assert structured["pkg_env_keys"] == ["GITHUB_MCP_AUTH_HEADER"]
        written = yaml.safe_load(_structured_to_yaml(structured))
        assert written["package"]["env_keys"] == ["GITHUB_MCP_AUTH_HEADER"]

    def test_survives_a_save_through_the_api(self, client, tools_dir):
        from frontend.app import _provider_to_structured

        structured = _provider_to_structured("ghcopilot", self.SPEC)
        r = client.post(
            "/api/tools", json={"name": "ghcopilot", "provider": structured}
        )
        assert r.status_code == 200, r.text
        written = yaml.safe_load((tools_dir / "ghcopilot.yaml").read_text())
        assert written["package"]["env_keys"] == ["GITHUB_MCP_AUTH_HEADER"]

    def test_absent_env_keys_are_not_invented(self):
        from frontend.app import _provider_to_structured, _structured_to_yaml

        spec = {"package": {"command": "npx @playwright/mcp"}, "tools": []}
        out = yaml.safe_load(_structured_to_yaml(_provider_to_structured("pw", spec)))
        assert "env_keys" not in out["package"]

    def test_repository_providers_keep_both_lists(self):
        from frontend.app import _provider_to_structured, _structured_to_yaml

        spec = {
            "package": {"command": "node dist/index.js", "env_keys": ["PKG_TOKEN"]},
            "repository": {"url": "https://github.com/x/y", "env_keys": ["REPO_TOKEN"]},
            "tools": [],
        }
        out = yaml.safe_load(_structured_to_yaml(_provider_to_structured("repo", spec)))
        assert out["package"]["env_keys"] == ["PKG_TOKEN"]
        assert out["repository"]["env_keys"] == ["REPO_TOKEN"]

    def test_editor_exposes_the_field(self, client):
        html = client.get("/").text
        for needle in ("pkg-env-keys-container", "addPkgEnvKey()", "renderPkgEnvKeys"):
            assert needle in html, needle


class TestUIStaysResponsiveDuringSlowWork:
    """The provider list, status polling and the pending-auth banner all come
    from the same event loop as introspection and cloning. A blocking
    subprocess.run in one of those routes froze the entire page."""

    def test_introspect_does_not_block_the_event_loop(self, client, monkeypatch):
        import threading

        loop_thread = threading.get_ident()
        ran_on: dict[str, int] = {}

        def slow_run(*args, **kwargs):
            ran_on["thread"] = threading.get_ident()
            return None

        monkeypatch.setattr("frontend.app.subprocess.run", slow_run)
        with patch("process_runner.introspect", new_callable=AsyncMock) as spawn:
            spawn.return_value = []
            r = client.post(
                "/api/introspect",
                json={
                    "command": "npx some-server",
                    "setup_commands": ["echo hi"],
                },
            )
        assert r.json()["ok"] is True
        # TestClient drives the loop from the calling thread; the blocking work
        # must have been handed to a worker.
        assert ran_on["thread"] != loop_thread

    def test_clone_and_build_does_not_block_the_event_loop(
        self, client, tmp_path, monkeypatch
    ):
        import threading

        loop_thread = threading.get_ident()
        threads: set[int] = set()

        def slow_run(*args, **kwargs):
            threads.add(threading.get_ident())
            return None

        monkeypatch.setattr("frontend.app.subprocess.run", slow_run)
        r = client.post(
            "/api/clone-and-build",
            json={
                "name": "demo",
                "repo_url": "https://example.com/x.git",
                "workdir": str(tmp_path / "wd"),
                "build_commands": ["echo build"],
            },
        )
        assert r.status_code == 200
        assert threads and loop_thread not in threads


class TestServerNameSetting:
    """MCP_SERVER_NAME is the display name reported to MCP clients. It affects
    nothing else — tool names come from each provider's YAML filename — so it is
    safe to edit, but it only takes effect on restart."""

    def test_config_reports_the_running_name(self, client):
        body = client.get("/api/config").json()
        assert body["server_name"]
        assert body["pending_server_name"] is None

    def test_saving_writes_the_env_file(self, client, env_path):
        r = client.post("/api/server-name", json={"name": "mcpproxy"})
        assert r.json()["ok"] is True
        assert "MCP_SERVER_NAME=mcpproxy" in Path(env_path).read_text()

    def test_a_saved_but_unapplied_name_is_reported_separately(self, client):
        # Saying "saved" without saying "not yet live" would be misleading:
        # clients keep seeing the old name until the process restarts.
        client.post("/api/server-name", json={"name": "something-else"})
        body = client.get("/api/config").json()
        assert body["pending_server_name"] == "something-else"
        assert body["server_name"] != "something-else"

    @pytest.mark.parametrize(
        "name", ["", "   ", "a; rm -rf /", "name\nwith-newline", "x" * 129]
    )
    def test_rejects_unusable_names(self, client, name):
        assert client.post("/api/server-name", json={"name": name}).status_code == 400

    @pytest.mark.parametrize("name", ["mcpproxy", "My Proxy", "mcp-proxy_2.0"])
    def test_accepts_ordinary_names(self, client, name):
        assert client.post("/api/server-name", json={"name": name}).status_code == 200

    def test_ui_exposes_the_setting(self, client):
        html = client.get("/").text
        for needle in ("settings-modal", "openSettings()", "saveSettings", "/api/server-name"):
            assert needle in html, needle


class TestSecretsDialogShowsDeclaredKeys:
    """The provider list badge and this dialog used to disagree: the badge counts
    the server's union of secret keys, while the dialog only read per-tool
    secrets — so a package provider's declared variable never appeared."""

    def test_package_env_keys_reach_the_api(self, client, tools_dir):
        (tools_dir / "ghcopilot.yaml").write_text(
            yaml.safe_dump(
                {
                    "package": {
                        "command": (
                            "npx -y mcp-remote https://api.githubcopilot.com/mcp/ "
                            "--header Authorization:${GITHUB_MCP_AUTH_HEADER}"
                        ),
                        "env_keys": ["GITHUB_MCP_AUTH_HEADER"],
                    },
                    "tools": [],
                }
            )
        )
        entry = next(
            p for p in client.get("/api/tools").json() if p["name"] == "ghcopilot"
        )
        assert entry["secret_keys"] == ["GITHUB_MCP_AUTH_HEADER"]
        assert entry["missing_secrets"] == ["GITHUB_MCP_AUTH_HEADER"]

    def test_dialog_seeds_from_the_same_list_the_badge_uses(self, client):
        html = client.get("/").text
        # openSecretsModal must start from meta.secret_keys, not only per-tool
        # secrets, or the two disagree again.
        assert "const keys = [...(meta.secret_keys || [])];" in html

    def test_token_header_bridges_are_not_offered_oauth_reauth(self, client):
        html = client.get("/").text
        assert "package-token-note" in html
        assert "usesTokenHeader" in html


class TestConcurrentSpawnIsAState:
    """The startup warm-up holds a bridge's spawn open for the whole
    authorization window, so every editor open and command-field blur meanwhile
    hits the concurrency guard. That is expected, not a crash."""

    def test_introspect_reports_it_without_a_traceback(self, client, capsys):
        import process_runner

        command = "npx -y mcp-remote https://mcp.asana.com/v2/mcp 8887"
        process_runner._spawning.add(command)
        process_runner.pending_auth_urls[command] = "https://app.asana.com/-/oauth"
        try:
            body = client.post("/api/introspect", json={"command": command}).json()
        finally:
            process_runner._spawning.discard(command)
            process_runner.pending_auth_urls.pop(command, None)

        assert body["ok"] is False
        assert body["pending_auth"] is True
        # The link that actually unblocks it comes back with the refusal.
        assert body["auth_url"] == "https://app.asana.com/-/oauth"
        assert "Traceback" not in capsys.readouterr().err

    def test_the_message_does_not_repeat_the_command(self, client):
        import process_runner

        command = (
            "npx -y mcp-remote https://mcp.asana.com/v2/mcp 8887 "
            "--static-oauth-client-info @/app/tools/secrets/asana/client_info.json"
        )
        process_runner._spawning.add(command)
        try:
            body = client.post("/api/introspect", json={"command": command}).json()
        finally:
            process_runner._spawning.discard(command)
        # Commands carry credential-file paths and are unreadable in a log line.
        assert "client_info.json" not in body["error"]

    def test_reauthorize_reports_it_rather_than_racing(self, client, tools_dir):
        import process_runner

        command = "npx -y mcp-remote https://mcp.asana.com/v2/mcp 8887"
        (tools_dir / "asana.yaml").write_text(
            yaml.safe_dump({"package": {"command": command}, "tools": []})
        )
        process_runner._spawning.add(command)
        try:
            body = client.post("/api/oauth-reauthorize", json={"target": "asana"}).json()
        finally:
            process_runner._spawning.discard(command)
        assert body["ok"] is False
        assert "already starting" in body["error"]


class TestManualCallbackTargetRequired:
    def test_missing_target_explains_what_to_do(self, client):
        r = client.post(
            "/api/oauth-manual-callback", json={"callback": "?code=abc&state=xyz"}
        )
        assert r.status_code == 400
        assert "Choose which provider" in r.json()["detail"]

    def test_dropdown_placeholders_are_not_selectable(self, client):
        # An empty target used to be POSTed while the list was still loading,
        # which came back as a bare 400.
        html = client.get("/").text
        assert '<option value="" disabled selected>loading…</option>' in html
        assert "Choose the provider this callback belongs to." in html


class TestRemoteCommandSummary:
    """The diagnostics table shows this. Commands carry credential-file paths."""

    def test_keeps_the_bridge_url_and_port_but_drops_the_flags(self):
        from frontend.app import _summarize_remote_command

        out = _summarize_remote_command(
            "npx -y mcp-remote@0.1.38 https://mcp.asana.com/v2/mcp 8887 "
            "--static-oauth-client-info @/app/tools/secrets/asana/client_info.json "
            "--resource https://mcp.asana.com/v2 --auth-timeout 600"
        )
        assert out == "mcp-remote@0.1.38 https://mcp.asana.com/v2/mcp 8887"
        assert "client_info.json" not in out

    def test_handles_a_command_with_no_flags(self):
        from frontend.app import _summarize_remote_command

        assert _summarize_remote_command("npx -y mcp-remote https://x/mcp") == (
            "mcp-remote https://x/mcp"
        )

    def test_does_not_crash_on_an_unexpected_command(self):
        from frontend.app import _summarize_remote_command

        assert isinstance(_summarize_remote_command(""), str)


class TestSecretEnvKeyUnion:
    """The provider list badge and the Secrets dialog both read this."""

    def test_unions_every_declaration_site_without_duplicates(self):
        from frontend.app import _extract_secret_env_keys

        spec = {
            "package": {"command": "node x", "env_keys": ["PKG", "SHARED"]},
            "repository": {"url": "https://x/y", "env_keys": ["REPO", "SHARED"]},
            "tools": [{"name": "t", "secrets": {"env": {"arg": "TOOL"}}}],
        }
        keys = _extract_secret_env_keys(spec)
        assert set(keys) == {"PKG", "SHARED", "REPO", "TOOL"}
        assert len(keys) == len(set(keys))

    def test_repository_env_keys_alone_are_surfaced(self):
        from frontend.app import _extract_secret_env_keys

        spec = {"repository": {"url": "https://x/y", "env_keys": ["REPO_TOKEN"]}, "tools": []}
        assert _extract_secret_env_keys(spec) == ["REPO_TOKEN"]


class TestEnvFileWriting:
    """Every reader of a .env file — this module, ProcessSession._build_env,
    docker-compose, dotenv — takes the *last* occurrence of a key."""

    def test_rewrites_the_last_duplicate_and_drops_the_rest(self, tmp_path):
        from frontend.app import _read_env_file, _write_env_file

        f = tmp_path / ".env"
        f.write_text("A=1\nB=2\nA=3\n")
        _write_env_file(f, {"A": "9"})
        # Rewriting the first left the stale later line winning, so the UI said
        # "saved" and the value never took effect.
        assert _read_env_file(f)["A"] == "9"
        assert f.read_text().count("A=") == 1

    @pytest.mark.parametrize("value", ["x\nMCP_SERVER_NAME=pwned", "x\rY=2"])
    def test_rejects_values_containing_line_breaks(self, tmp_path, value):
        from frontend.app import _write_env_file

        # A pasted credential with a newline would define arbitrary variables
        # that _build_env then injects into every spawned subprocess.
        f = tmp_path / ".env"
        with pytest.raises(ValueError, match="line break"):
            _write_env_file(f, {"C": value})
        assert "pwned" not in (f.read_text() if f.exists() else "")

    def test_rejects_a_non_string_value(self, tmp_path):
        from frontend.app import _write_env_file

        with pytest.raises(ValueError, match="must be text"):
            _write_env_file(tmp_path / ".env", {"C": {"nested": "dict"}})

    def test_preserves_comments_and_unrelated_keys(self, tmp_path):
        from frontend.app import _read_env_file, _write_env_file

        f = tmp_path / ".env"
        f.write_text("# a comment\nKEEP=yes\nTARGET=old\n")
        _write_env_file(f, {"TARGET": "new"})
        text = f.read_text()
        assert "# a comment" in text
        assert _read_env_file(f) == {"KEEP": "yes", "TARGET": "new"}


class TestOutputEscaping:
    """Tool names are auto-filled from the remote MCP server being introspected,
    and provider names come from filenames in a mounted volume."""

    def test_esc_covers_single_quotes(self, client):
        html = client.get("/").text
        # esc() is interpolated into single-quoted attributes such as
        # onclick="openProvider('...')".
        assert ".replace(/'/g,'&#39;')" in html

    def test_provider_list_escapes_name_and_tool_names(self, client):
        html = client.get("/").text
        assert """onclick="openProvider('${esc(p.name)}')\"""" in html
        assert '<div class="fw-semibold">${esc(p.name)}</div>' in html
        assert "${esc((p.tool_names || []).join(', ')) || 'no tools'}" in html

    def test_secret_keys_are_escaped_in_both_dialogs(self, client):
        html = client.get("/").text
        assert 'id="secret-${k}"' not in html
        assert html.count('id="secret-${esc(k)}"') >= 2

    def test_server_supplied_error_text_is_escaped(self, client):
        html = client.get("/").text
        assert 'style="font-size:.85em">${e.message}</div>' not in html


class TestReauthorizeRaceSafety:
    def test_a_pending_url_popped_mid_request_does_not_500(self, client, tools_dir):
        """pending_auth_urls is written and popped from other threads' event
        loops; testing membership then subscripting could raise KeyError."""
        import process_runner
        from frontend.app import _summarize_remote_command  # noqa: F401

        command = "npx -y mcp-remote https://mcp.asana.com/v2/mcp 8887"
        (tools_dir / "asana.yaml").write_text(
            yaml.safe_dump({"package": {"command": command}, "tools": []})
        )

        class VanishingDict(dict):
            def get(self, key, default=None):
                value = super().get(key, default)
                super().pop(key, None)          # popped by another thread
                return value

        original = process_runner.pending_auth_urls
        process_runner.pending_auth_urls = VanishingDict(
            {command: "https://app.asana.com/-/oauth"}
        )
        try:
            r = client.post("/api/oauth-reauthorize", json={"target": "asana"})
        finally:
            process_runner.pending_auth_urls = original
        assert r.status_code == 200
        assert r.json()["auth_url"] == "https://app.asana.com/-/oauth"

    def test_delivery_log_does_not_echo_the_spawn_command(self, client, capsys):
        """When the caller picks a pending flow the target *is* the command,
        which carries header credentials and client-secret file paths."""
        import process_runner

        command = (
            "npx -y mcp-remote https://mcp.asana.com/v2/mcp 8887 "
            "--static-oauth-client-info @/app/tools/secrets/asana/client_info.json"
        )
        process_runner.pending_auth_urls[command] = "https://app.asana.com/-/oauth"
        try:
            with patch(
                "oauth_callback_relay.deliver_to_bridge", new_callable=AsyncMock
            ) as deliver:
                deliver.return_value = 200
                client.post(
                    "/api/oauth-manual-callback",
                    json={"target": command, "callback": "?code=abc"},
                )
        finally:
            process_runner.pending_auth_urls.pop(command, None)
        assert "client_info.json" not in capsys.readouterr().out

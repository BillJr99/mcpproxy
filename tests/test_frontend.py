"""Unit tests for the HTTP frontend (frontend/app.py)."""
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import yaml
from fastapi.testclient import TestClient

from frontend.app import (
    _detect_package_manager,
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


class TestExtractSecretEnvKeysRest:
    def test_rest_auth_env_keys_extracted(self):
        spec = yaml.safe_load(_structured_to_yaml(REST_PROVIDER))
        keys = _extract_secret_env_keys(spec)
        assert "WEATHER_CLIENT_ID" in keys
        assert "WEATHER_CLIENT_SECRET" in keys


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

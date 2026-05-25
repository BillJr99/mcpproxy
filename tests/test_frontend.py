"""Unit tests for the HTTP frontend (frontend/app.py)."""
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

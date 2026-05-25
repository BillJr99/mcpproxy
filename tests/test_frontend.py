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
    _provider_to_structured,
    _read_env_file,
    _structured_to_yaml,
    _validate_provider,
    _write_env_file,
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
        "documentation": "", "parameters": [], "secrets": [],
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
        "documentation": "", "parameters": [
            {"name": "url", "type": "string", "description": "URL", "required": True, "default": None}
        ], "secrets": [],
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
        assert "Discover" not in client.get("/").text

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

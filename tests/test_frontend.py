"""Unit tests for the HTTP frontend (frontend/app.py)."""
import os
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from frontend.app import (
    _extract_secret_env_keys,
    _read_env_file,
    _skeleton_from_code,
    _skeleton_from_repo,
    _validate_spec,
    _write_env_file,
    create_app,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def app(tmp_path: Path):
    """A fresh app instance backed by a temp config dir and env file."""
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    env_file = tmp_path / ".env"
    return create_app(config_dir=tools_dir, env_file=env_file)


@pytest.fixture()
def client(app):
    return TestClient(app)


@pytest.fixture()
def tools_dir(tmp_path: Path) -> Path:
    d = tmp_path / "tools"
    d.mkdir()
    return d


@pytest.fixture()
def env_path(tmp_path: Path) -> Path:
    return tmp_path / ".env"


VALID_YAML = """\
code: |
  from typing import Any
  async def ping(context: dict, msg: str = 'hi') -> dict:
      return {'ok': True}

tools:
  - name: ping
    function: ping
    description: Ping tool
    input_schema:
      type: object
      properties:
        msg:
          type: string
      required: []
"""

SECRET_YAML = """\
code: |
  async def my_tool(context, key: str) -> dict:
      return {'ok': True}

tools:
  - name: my_tool
    function: my_tool
    description: Tool with secret
    input_schema:
      type: object
      properties: {}
      required: []
    secrets:
      env:
        key: MY_SECRET_KEY
"""


# ---------------------------------------------------------------------------
# GET /api/tools
# ---------------------------------------------------------------------------

class TestListTools:
    def test_empty_dir(self, client):
        r = client.get("/api/tools")
        assert r.status_code == 200
        assert r.json() == []

    def test_lists_provider(self, app, tmp_path):
        d = tmp_path / "tools"
        (d / "myprovider.yaml").write_text(VALID_YAML)
        c = TestClient(app)
        r = c.get("/api/tools")
        assert r.status_code == 200
        data = r.json()
        assert len(data) == 1
        assert data[0]["name"] == "myprovider"
        assert data[0]["tool_count"] == 1
        assert data[0]["has_repo"] is False

    def test_shows_repo_flag(self, app, tmp_path):
        d = tmp_path / "tools"
        spec = {"repo": {"url": "https://github.com/x/y"}, "code": "pass", "tools": []}
        (d / "repo_provider.yaml").write_text(yaml.dump(spec))
        c = TestClient(app)
        r = c.get("/api/tools")
        assert r.json()[0]["has_repo"] is True


# ---------------------------------------------------------------------------
# GET /api/tools/{name}
# ---------------------------------------------------------------------------

class TestGetTool:
    def test_existing(self, app, tmp_path):
        (tmp_path / "tools" / "alpha.yaml").write_text(VALID_YAML)
        r = TestClient(app).get("/api/tools/alpha")
        assert r.status_code == 200
        assert r.json()["content"] == VALID_YAML

    def test_not_found_404(self, client):
        assert client.get("/api/tools/nope").status_code == 404

    def test_secret_keys_returned(self, app, tmp_path):
        (tmp_path / "tools" / "s.yaml").write_text(SECRET_YAML)
        r = TestClient(app).get("/api/tools/s")
        assert "MY_SECRET_KEY" in r.json()["secret_keys"]


# ---------------------------------------------------------------------------
# POST /api/tools
# ---------------------------------------------------------------------------

class TestCreateTool:
    def test_create_new(self, client):
        r = client.post("/api/tools", json={"name": "newprovider", "content": VALID_YAML})
        assert r.status_code == 200
        assert r.json()["ok"] is True

    def test_duplicate_returns_409(self, app, tmp_path):
        (tmp_path / "tools" / "dupe.yaml").write_text(VALID_YAML)
        r = TestClient(app).post("/api/tools", json={"name": "dupe", "content": VALID_YAML})
        assert r.status_code == 409

    def test_missing_name_400(self, client):
        assert client.post("/api/tools", json={"content": VALID_YAML}).status_code == 400

    def test_invalid_name_400(self, client):
        r = client.post("/api/tools", json={"name": "../evil", "content": VALID_YAML})
        assert r.status_code == 400

    def test_bad_yaml_400(self, client):
        r = client.post("/api/tools", json={"name": "x", "content": ": : bad yaml {{"})
        assert r.status_code == 400

    def test_invalid_structure_400(self, client):
        r = client.post("/api/tools", json={"name": "x", "content": "code: 'ok'\n"})
        assert r.status_code == 400

    def test_secret_keys_returned(self, client):
        r = client.post("/api/tools", json={"name": "sec", "content": SECRET_YAML})
        assert r.status_code == 200
        assert "MY_SECRET_KEY" in r.json()["secret_keys"]


# ---------------------------------------------------------------------------
# PUT /api/tools/{name}
# ---------------------------------------------------------------------------

class TestUpdateTool:
    def test_update_existing(self, app, tmp_path):
        (tmp_path / "tools" / "update_me.yaml").write_text(VALID_YAML)
        r = TestClient(app).put("/api/tools/update_me", json={"content": VALID_YAML + "\n"})
        assert r.status_code == 200

    def test_creates_if_not_exists(self, client):
        # PUT is upsert — creates if missing
        r = client.put("/api/tools/brand_new", json={"content": VALID_YAML})
        assert r.status_code == 200

    def test_bad_yaml_400(self, client):
        r = client.put("/api/tools/x", json={"content": ": bad"})
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# DELETE /api/tools/{name}
# ---------------------------------------------------------------------------

class TestDeleteTool:
    def test_delete_existing(self, app, tmp_path):
        p = tmp_path / "tools" / "todelete.yaml"
        p.write_text(VALID_YAML)
        r = TestClient(app).delete("/api/tools/todelete")
        assert r.status_code == 200
        assert not p.exists()

    def test_delete_missing_404(self, client):
        assert client.delete("/api/tools/ghost").status_code == 404


# ---------------------------------------------------------------------------
# POST /api/validate
# ---------------------------------------------------------------------------

class TestValidate:
    def test_valid_yaml(self, client):
        r = client.post("/api/validate", json={"content": VALID_YAML})
        assert r.status_code == 200
        assert r.json()["ok"] is True
        assert r.json()["errors"] == []

    def test_parse_error(self, client):
        r = client.post("/api/validate", json={"content": ": : : bad"})
        assert not r.json()["ok"]
        assert any("parse" in e.lower() for e in r.json()["errors"])

    def test_missing_tools_list(self, client):
        r = client.post("/api/validate", json={"content": "code: 'pass'\n"})
        assert not r.json()["ok"]

    def test_missing_code_and_repo(self, client):
        r = client.post("/api/validate", json={"content": "tools: []\n"})
        assert not r.json()["ok"]

    def test_tool_missing_name(self, client):
        bad = "code: 'pass'\ntools:\n  - function: fn\n    description: d\n    input_schema: {type: object}\n"
        r = client.post("/api/validate", json={"content": bad})
        assert not r.json()["ok"]

    def test_repo_block_replaces_code(self, client):
        content = "repo:\n  url: https://github.com/x/y\ntools:\n  - name: t\n    function: f\n    description: d\n    input_schema:\n      type: object\n"
        r = client.post("/api/validate", json={"content": content})
        # Should not complain about missing code when repo is present
        errors = r.json()["errors"]
        code_errors = [e for e in errors if "code" in e.lower() and "repo" not in e.lower()]
        assert len(code_errors) == 0


# ---------------------------------------------------------------------------
# POST /api/generate-skeleton
# ---------------------------------------------------------------------------

class TestGenerateSkeleton:
    def test_blank(self, client):
        r = client.post("/api/generate-skeleton", json={"source": "blank"})
        assert r.status_code == 200
        assert "code:" in r.json()["yaml"]
        assert "tools:" in r.json()["yaml"]

    def test_from_code(self, client):
        code = "async def greet(context, name: str) -> dict:\n    return {'ok': True}\n"
        r = client.post("/api/generate-skeleton", json={"source": "code", "code": code})
        assert r.status_code == 200
        assert "greet" in r.json()["yaml"]

    def test_from_repo(self, client):
        r = client.post("/api/generate-skeleton", json={
            "source": "repo",
            "repo_url": "https://github.com/user/my-mcp",
            "branch": "main",
        })
        assert r.status_code == 200
        j = r.json()
        assert "repo:" in j["yaml"]
        assert "https://github.com/user/my-mcp" in j["yaml"]

    def test_unknown_source_400(self, client):
        assert client.post("/api/generate-skeleton", json={"source": "magic"}).status_code == 400


# ---------------------------------------------------------------------------
# POST /api/extract-functions
# ---------------------------------------------------------------------------

class TestExtractFunctions:
    def test_finds_async_with_context(self, client):
        code = "async def my_fn(context, x: str) -> dict:\n    pass\n"
        r = client.post("/api/extract-functions", json={"code": code})
        assert r.status_code == 200
        fns = r.json()["functions"]
        assert len(fns) == 1
        assert fns[0]["name"] == "my_fn"
        assert fns[0]["params"] == [{"name": "x", "type": "string"}]

    def test_skips_fn_without_context(self, client):
        code = "async def no_ctx(x: str) -> dict:\n    pass\n"
        r = client.post("/api/extract-functions", json={"code": code})
        assert r.json()["functions"] == []

    def test_syntax_error(self, client):
        r = client.post("/api/extract-functions", json={"code": "def broken(: pass"})
        assert not r.json()["ok"]

    def test_multiple_functions(self, client):
        code = (
            "async def a(context, x): pass\n"
            "async def b(context, y, z): pass\n"
        )
        fns = client.post("/api/extract-functions", json={"code": code}).json()["functions"]
        names = [f["name"] for f in fns]
        assert "a" in names and "b" in names


# ---------------------------------------------------------------------------
# GET /api/known-servers
# ---------------------------------------------------------------------------

class TestKnownServers:
    def test_returns_list(self, client):
        r = client.get("/api/known-servers")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)
        assert len(data) > 0
        assert all("name" in s and "description" in s for s in data)


# ---------------------------------------------------------------------------
# GET & POST /api/env
# ---------------------------------------------------------------------------

class TestEnvEndpoints:
    def test_get_env_empty(self, client):
        r = client.get("/api/env")
        assert r.status_code == 200
        assert "vars" in r.json()

    def test_set_and_get(self, app, tmp_path):
        env_file = tmp_path / ".env"
        c = TestClient(create_app(config_dir=tmp_path / "tools", env_file=env_file))
        (tmp_path / "tools").mkdir(exist_ok=True)
        r = c.post("/api/env", json={"vars": {"MY_KEY": "abc123"}})
        assert r.status_code == 200
        assert "MY_KEY" in r.json()["written"]
        assert env_file.exists()
        assert "MY_KEY=abc123" in env_file.read_text()

    def test_get_masks_values(self, app, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("SECRET_TOKEN=plaintext\n")
        (tmp_path / "tools").mkdir(exist_ok=True)
        c = TestClient(create_app(config_dir=tmp_path / "tools", env_file=env_file))
        r = c.get("/api/env")
        assert r.json()["vars"]["SECRET_TOKEN"] == "***"

    def test_invalid_key_name_400(self, client):
        r = client.post("/api/env", json={"vars": {"bad-key": "value"}})
        assert r.status_code == 400

    def test_lowercase_key_400(self, client):
        r = client.post("/api/env", json={"vars": {"lowercase": "value"}})
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# GET /  (HTML)
# ---------------------------------------------------------------------------

class TestHTML:
    def test_returns_html(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        assert "mcpproxy" in r.text.lower()

    def test_contains_api_calls(self, client):
        r = client.get("/")
        assert "/api/tools" in r.text


# ---------------------------------------------------------------------------
# Pure function tests (no HTTP)
# ---------------------------------------------------------------------------

class TestReadWriteEnvFile:
    def test_read_missing_file(self, tmp_path):
        assert _read_env_file(tmp_path / "no.env") == {}

    def test_read_basic(self, tmp_path):
        p = tmp_path / ".env"
        p.write_text("FOO=bar\nBAZ=qux\n")
        assert _read_env_file(p) == {"FOO": "bar", "BAZ": "qux"}

    def test_read_ignores_comments(self, tmp_path):
        p = tmp_path / ".env"
        p.write_text("# comment\nFOO=bar\n")
        assert _read_env_file(p) == {"FOO": "bar"}

    def test_write_creates_file(self, tmp_path):
        p = tmp_path / ".env"
        _write_env_file(p, {"KEY": "val"})
        assert "KEY=val" in p.read_text()

    def test_write_updates_existing(self, tmp_path):
        p = tmp_path / ".env"
        p.write_text("FOO=old\n")
        _write_env_file(p, {"FOO": "new"})
        content = p.read_text()
        assert "FOO=new" in content
        assert "FOO=old" not in content

    def test_write_appends_new_key(self, tmp_path):
        p = tmp_path / ".env"
        p.write_text("EXISTING=yes\n")
        _write_env_file(p, {"NEW_KEY": "added"})
        content = p.read_text()
        assert "EXISTING=yes" in content
        assert "NEW_KEY=added" in content


class TestExtractSecretEnvKeys:
    def test_empty_spec(self):
        assert _extract_secret_env_keys({}) == []

    def test_no_secrets(self):
        spec = {"tools": [{"name": "t", "input_schema": {}}]}
        assert _extract_secret_env_keys(spec) == []

    def test_single_secret(self):
        spec = {"tools": [{"secrets": {"env": {"key": "MY_KEY"}}}]}
        assert _extract_secret_env_keys(spec) == ["MY_KEY"]

    def test_deduplicates(self):
        spec = {
            "tools": [
                {"secrets": {"env": {"k": "SHARED"}}},
                {"secrets": {"env": {"k": "SHARED"}}},
            ]
        }
        keys = _extract_secret_env_keys(spec)
        assert keys.count("SHARED") == 1


class TestValidateSpec:
    def test_valid(self):
        result = _validate_spec(VALID_YAML)
        assert result["ok"] is True

    def test_not_a_dict(self):
        result = _validate_spec("- item1\n- item2\n")
        assert not result["ok"]

    def test_no_code_no_repo(self):
        result = _validate_spec("tools:\n  []\n")
        assert any("code" in e for e in result["errors"])


class TestSkeletonFromCode:
    def test_generates_yaml(self):
        code = "async def my_fn(context, x: str) -> dict:\n    return {}\n"
        result = _skeleton_from_code(code)
        assert "my_fn" in result
        assert "tools:" in result

    def test_syntax_error_returns_blank(self):
        result = _skeleton_from_code("def broken(: pass")
        assert "tools:" in result  # returns blank template

    def test_no_async_returns_blank(self):
        result = _skeleton_from_code("def sync_fn(context, x): pass")
        assert "tools:" in result


class TestSkeletonFromRepo:
    def test_basic(self):
        body = {"repo_url": "https://github.com/user/my-repo", "branch": "main"}
        result = _skeleton_from_repo(body)
        assert "repo:" in result
        assert "https://github.com/user/my-repo" in result
        assert "main" in result

    def test_with_packages(self):
        body = {
            "repo_url": "https://github.com/x/y",
            "packages": ["requests", "httpx"],
        }
        result = _skeleton_from_repo(body)
        assert "install_packages" in result
        assert "requests" in result

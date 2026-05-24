"""
Pytest configuration.

Sets MCP_TOOL_CONFIG_DIR, MCP_REPOS_DIR, and MCP_ENV_FILE to temp directories
BEFORE any module-level code in server.py or config.py runs (server.py calls
load_provider_specs at import time, which does a glob — must not fail).
"""
import os
import tempfile

# ── Set env vars before any project imports ─────────────────────────────────
_tmp_config = tempfile.mkdtemp(prefix="mcpproxy_config_")
_tmp_repos = tempfile.mkdtemp(prefix="mcpproxy_repos_")
_tmp_env = os.path.join(tempfile.mkdtemp(prefix="mcpproxy_env_"), ".env")

os.environ.setdefault("MCP_TOOL_CONFIG_DIR", _tmp_config)
os.environ.setdefault("MCP_REPOS_DIR", _tmp_repos)
os.environ.setdefault("MCP_ENV_FILE", _tmp_env)
os.environ.setdefault("MCP_UI_PORT", "18889")  # avoid clashing with a running server

import pytest
from pathlib import Path


@pytest.fixture()
def config_dir(tmp_path: Path) -> Path:
    """A fresh empty config directory for each test."""
    d = tmp_path / "tools"
    d.mkdir()
    return d


@pytest.fixture()
def env_file(tmp_path: Path) -> Path:
    """A fresh .env file path for each test (file does not exist yet)."""
    return tmp_path / ".env"


@pytest.fixture()
def repos_dir(tmp_path: Path) -> Path:
    """A fresh repos directory for each test."""
    d = tmp_path / "repos"
    d.mkdir()
    return d


@pytest.fixture()
def simple_yaml_spec() -> dict:
    """A minimal valid provider spec (without _config_path)."""
    return {
        "code": (
            "from typing import Any\n"
            "async def ping(context, message: str = 'hi') -> dict:\n"
            "    return {'ok': True, 'echo': message}\n"
        ),
        "tools": [
            {
                "name": "ping",
                "function": "ping",
                "description": "Echo a message",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "message": {"type": "string", "description": "msg"},
                    },
                    "required": [],
                },
            }
        ],
    }

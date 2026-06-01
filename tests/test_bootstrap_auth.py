"""Tests for the mcp-remote token-cache bootstrap automation.

Covers the pure command-extraction helpers used by both the host bootstrap
script (bootstrap_auth.py) and the server startup warm-up (server.py).
"""
import yaml

import bootstrap_auth
import server


# ---------------------------------------------------------------------------
# bootstrap_auth.extract_remote_commands / command_url
# ---------------------------------------------------------------------------

class TestExtractRemoteCommands:
    def test_finds_mcp_remote_commands(self):
        specs = [
            {"package": {"command": "npx -y mcp-remote https://mcp.asana.com/v2/mcp"}},
            {"package": {"command": "uvx mcp-server-fetch"}},   # not a bridge
            {"code": "async def f(): ..."},                       # code provider
        ]
        assert bootstrap_auth.extract_remote_commands(specs) == [
            "npx -y mcp-remote https://mcp.asana.com/v2/mcp"
        ]

    def test_deduplicates(self):
        cmd = "npx -y mcp-remote https://x/mcp"
        specs = [{"package": {"command": cmd}}, {"package": {"command": cmd}}]
        assert bootstrap_auth.extract_remote_commands(specs) == [cmd]

    def test_empty_when_no_packages(self):
        assert bootstrap_auth.extract_remote_commands([{}, {"code": "x"}]) == []

    def test_command_url_extracts_first_http_token(self):
        assert (bootstrap_auth.command_url("npx -y mcp-remote https://mcp.asana.com/v2/mcp")
                == "https://mcp.asana.com/v2/mcp")

    def test_command_url_none_when_absent(self):
        assert bootstrap_auth.command_url("npx -y mcp-remote") is None


# ---------------------------------------------------------------------------
# server warm-up helpers
# ---------------------------------------------------------------------------

class TestServerWarmup:
    def test_remote_bridge_commands_reads_config_dir(self, tmp_path, monkeypatch):
        (tmp_path / "asana.yaml").write_text(
            yaml.safe_dump({"package": {"command": "npx -y mcp-remote https://mcp.asana.com/v2/mcp"}}),
            encoding="utf-8",
        )
        (tmp_path / "fetch.yaml").write_text(
            yaml.safe_dump({"package": {"command": "uvx mcp-server-fetch"}}),
            encoding="utf-8",
        )
        monkeypatch.setattr(server, "CONFIG_DIR", tmp_path)
        assert server._remote_bridge_commands() == [
            "npx -y mcp-remote https://mcp.asana.com/v2/mcp"
        ]

    def test_warm_enabled_default(self, monkeypatch):
        monkeypatch.delenv("MCPPROXY_WARM_REMOTE", raising=False)
        assert server._warm_remote_enabled() is True

    def test_warm_disabled(self, monkeypatch):
        for val in ("0", "false", "no", "off", ""):
            monkeypatch.setenv("MCPPROXY_WARM_REMOTE", val)
            assert server._warm_remote_enabled() is False

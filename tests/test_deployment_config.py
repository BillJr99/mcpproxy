"""Deployment-contract tests for OAuth callback and token persistence."""

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parent.parent


def _compose_service() -> dict:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    return compose["services"]["mcp-host"]


def test_image_documents_static_callback_port():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "EXPOSE 8888 8889 8887" in dockerfile


def test_compose_publishes_callback_only_on_host_loopback():
    ports = _compose_service()["ports"]
    assert "127.0.0.1:${MCP_OAUTH_CALLBACK_PORT:-8887}:8887" in ports


def test_compose_enables_callback_forwarder_and_persistent_auth_dir():
    environment = _compose_service()["environment"]
    assert environment["MCPPROXY_CALLBACK_FORWARD_PORTS"] == "8887"
    assert environment["MCP_REMOTE_CONFIG_DIR"] == "/app/.mcp-auth"


def test_compose_persists_mcp_remote_auth_cache():
    mounts = _compose_service()["volumes"]
    assert "mcpproxy-mcp-auth:/app/.mcp-auth" in mounts

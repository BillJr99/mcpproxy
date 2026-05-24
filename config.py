"""Shared configuration — imported by both server.py and frontend/app.py."""
import os
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("MCP_TOOL_CONFIG_DIR", "/app/tools"))
REPOS_DIR = Path(os.environ.get("MCP_REPOS_DIR", "/app/repos"))
ENV_FILE = Path(os.environ.get("MCP_ENV_FILE", ".env"))
SERVER_NAME = os.environ.get("MCP_SERVER_NAME", "local-config-driven-mcp")

UI_HOST = os.environ.get("MCP_UI_HOST", "0.0.0.0")
UI_PORT = int(os.environ.get("MCP_UI_PORT", "8889"))

MCP_HOST = os.environ.get("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.environ.get("MCP_PORT", "8888"))

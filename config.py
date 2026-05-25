"""Shared configuration — imported by both server.py and frontend/app.py."""
import os
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("MCP_TOOL_CONFIG_DIR", "/app/tools"))
ENV_FILE = Path(os.environ.get("MCP_ENV_FILE", ".env"))
SERVER_NAME = os.environ.get("MCP_SERVER_NAME", "local-config-driven-mcp")

# Base directory exposed by the built-in mcpproxy__listfiles / mcpproxy__getfile tools.
# Defaults to .playwright-mcp (relative to the server's working directory) so that
# screenshots and snapshots produced by the Playwright MCP package provider are
# immediately accessible.  Override with MCPPROXY_FILES_DIR.
FILES_DIR = Path(os.environ.get("MCPPROXY_FILES_DIR", ".playwright-mcp"))

# Base directory where repository providers clone their git repos.  Each
# provider gets a subdirectory named after the provider (e.g. /app/repos/linkedin).
# Override with MCPPROXY_REPOS_DIR.
REPOS_DIR = Path(os.environ.get("MCPPROXY_REPOS_DIR", "/app/repos"))

UI_HOST = os.environ.get("MCP_UI_HOST", "0.0.0.0")
UI_PORT = int(os.environ.get("MCP_UI_PORT", "8889"))

MCP_HOST = os.environ.get("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.environ.get("MCP_PORT", "8888"))

"""Shared configuration — imported by both server.py and frontend/app.py."""
import os
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("MCP_TOOL_CONFIG_DIR", "/app/tools"))
ENV_FILE = Path(os.environ.get("MCP_ENV_FILE", ".env"))
SERVER_NAME = os.environ.get("MCP_SERVER_NAME", "local-config-driven-mcp")

# Base directory exposed by the built-in mcpproxy__listfiles / mcpproxy__getfile tools.
# Defaults to /app/files inside Docker so the directory can be mounted as a volume to
# persist screenshots, snapshots, and other artefacts produced by package providers
# (e.g. Playwright MCP writing under /app/files/playwright when launched with
# `--output-dir /app/files/playwright`).  Override with MCPPROXY_FILES_DIR (run_local.sh
# sets it to ./files for local non-Docker runs).
FILES_DIR = Path(os.environ.get("MCPPROXY_FILES_DIR", "/app/files"))

# Base directory where repository providers clone their git repos.  Each
# provider gets a subdirectory named after the provider (e.g. /app/repos/linkedin).
# Override with MCPPROXY_REPOS_DIR.
REPOS_DIR = Path(os.environ.get("MCPPROXY_REPOS_DIR", "/app/repos"))

# Directory where REST providers cache OAuth tokens (authorization_code flow).
# One JSON file per provider (e.g. /app/.rest-auth/<provider>.json) holding the
# access/refresh tokens and expiry.  Gitignored.  Override with
# MCPPROXY_REST_AUTH_DIR (run_local.sh points it at ./.rest-auth for local runs).
REST_AUTH_DIR = Path(os.environ.get("MCPPROXY_REST_AUTH_DIR", "/app/.rest-auth"))

# Public base URL the OAuth provider redirects back to after the user authorizes
# a REST provider's authorization_code flow.  The callback route is served by the
# UI app at "<base>/oauth/callback", so this must match a redirect URI registered
# with the OAuth provider.  Override with MCPPROXY_OAUTH_REDIRECT_BASE.
OAUTH_REDIRECT_BASE = os.environ.get(
    "MCPPROXY_OAUTH_REDIRECT_BASE", "http://localhost:8889"
).rstrip("/")

UI_HOST = os.environ.get("MCP_UI_HOST", "0.0.0.0")
UI_PORT = int(os.environ.get("MCP_UI_PORT", "8889"))

MCP_HOST = os.environ.get("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.environ.get("MCP_PORT", "8888"))

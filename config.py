"""Shared configuration — imported by both server.py and frontend/app.py."""
import os
import re
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("MCP_TOOL_CONFIG_DIR", "/app/tools"))
ENV_FILE = Path(os.environ.get("MCP_ENV_FILE", ".env"))
SERVER_NAME = os.environ.get("MCP_SERVER_NAME", "local-config-driven-mcp")

# Base directory exposed by the built-in mcpproxy__listfiles / mcpproxy__getfile /
# mcpproxy__deletefile tools.
# Defaults to /app/files inside Docker so the directory can be mounted as a volume to
# persist screenshots, snapshots, and other artefacts produced by package providers
# (e.g. Playwright MCP writing under /app/files/playwright when launched with
# `--output-dir /app/files/playwright`).  Override with MCPPROXY_FILES_DIR (run_local.sh
# sets it to ./files for local non-Docker runs).
#
# mcpproxy__deletefile is the only built-in that mutates this directory.  It is
# registered by default; set MCPPROXY_ENABLE_DELETEFILE=0 to withhold it and keep the
# built-in file surface read-only (see server._delete_file_enabled).
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


# ---------------------------------------------------------------------------
# .env value quoting
# ---------------------------------------------------------------------------
#
# The env file is consumed three ways: parsed by this project, handed to
# docker-compose as env_file, and *sourced by a shell* (run_local.sh does
# `set -a; source "$ENV_FILE"`).  That last one is why a value needs quoting:
# `TOKEN=Bearer ghp_x` assigns only "Bearer" and then tries to run `ghp_x`.
# Writing values raw therefore silently truncated anything with a space.

_NEEDS_QUOTING = re.compile(r"""[\s"'#$`\\]""")

# Inside double quotes the shell still acts on these, so they are backslashed.
_SHELL_SPECIAL = '\\"$`'


def env_quote(value: str) -> str:
    """Render *value* for the right-hand side of a .env line.

    Quoted only when it would otherwise be misread, so ordinary settings stay
    readable — matching how .env.example is written.
    """
    if value == "":
        return '""'
    if not _NEEDS_QUOTING.search(value):
        return value
    escaped = value
    for char in _SHELL_SPECIAL:
        escaped = escaped.replace(char, "\\" + char)
    return f'"{escaped}"'


def env_unquote(raw: str) -> str:
    """Inverse of :func:`env_quote`, tolerant of hand-written files.

    Single quotes are literal to the shell, so their body is taken as-is;
    double quotes undo the escaping ``env_quote`` applies.
    """
    text = raw.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        body = text[1:-1]
        if text[0] == "'":
            return body
        out: list[str] = []
        i = 0
        while i < len(body):
            char = body[i]
            if char == "\\" and i + 1 < len(body) and body[i + 1] in _SHELL_SPECIAL:
                out.append(body[i + 1])
                i += 2
            else:
                out.append(char)
                i += 1
        return "".join(out)
    return text

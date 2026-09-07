"""Shared configuration — imported by both server.py and frontend/app.py."""
import os
import re
import threading
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


# ---------------------------------------------------------------------------
# .env reading, and keeping os.environ in step with the file
# ---------------------------------------------------------------------------
#
# The env file is the source of truth for secrets, but nothing used to re-read
# it: os.environ is populated once, externally (docker-compose `env_file:`, or
# run_local.sh doing `set -a; source`).  A value edited in the Secrets UI wrote
# the file and changed nothing else, so a rotated credential kept failing until
# the container was restarted.
#
# refresh_env() closes that gap.  It re-reads the file when it has changed and
# updates os.environ, so every existing consumer -- rest_provider._require_env,
# server.resolve_env_defaults, and each subprocess spawned with os.environ.copy()
# -- sees the new value without a restart.
#
# Secret hygiene: values live only in os.environ and the returned mapping.
# Nothing here logs, raises, or returns a secret *value*; callers are given key
# NAMES only, matching the discipline in rest_provider._require_env and
# process_runner.check_header_credentials.


def read_env_file(path: "Path | str") -> dict[str, str]:
    """Parse a .env file into a mapping, applying :func:`env_unquote`.

    The single canonical parser.  Every reader has to agree with every other
    one, and with the shell that sources this same file, so the rules live
    here rather than being reimplemented per call site: blank lines and
    comments are skipped, a line without ``=`` is skipped, the key is
    stripped, only the first ``=`` splits, and the *last* assignment of a key
    wins (which is what the shell and docker-compose both do).

    A missing file is not an error; it reads as empty.
    """
    result: dict[str, str] = {}
    p = Path(path)
    if not p.exists():
        return result
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        result[key.strip()] = env_unquote(val)
    return result


# Guards the cache below.  A threading.Lock, not an asyncio.Lock: the env file
# is reached from the UI event loop, the MCP event loop, and plain background
# threads (startup warm-up, provider setup).  An asyncio.Lock binds to one loop
# and cannot protect the thread callers at all.
_env_lock = threading.Lock()

# (stat stamp, parsed mapping) for the last read of the env file, or None.
_env_cache: tuple[tuple[int, int, int], dict[str, str]] | None = None

# Keys this module has written into os.environ, and the value it wrote.  Used
# to retire a key that is later deleted from the file without clobbering an
# unrelated process variable that happens to share its name.
_env_injected: dict[str, str] = {}


def _env_file_path() -> Path:
    """The env file to track, re-read each call so tests can repoint it."""
    return Path(os.environ.get("MCP_ENV_FILE", ".env"))


def _env_stamp(path: Path) -> tuple[int, int, int] | None:
    """Identity of the file's current contents, or None if it is absent.

    Size and inode accompany the timestamp because mtime alone is too coarse:
    a save-then-read inside the same filesystem tick would otherwise look
    unchanged.  Callers that know they just wrote the file should still pass
    ``force=True`` rather than rely on this.
    """
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size, st.st_ino)


def refresh_env(force: bool = False) -> list[str]:
    """Sync os.environ from the env file; return the names that changed.

    Cheap to call on every tool dispatch: one ``stat`` unless the file has
    actually changed since the last read.  Pass *force* to re-read regardless,
    which the Secrets endpoint does after writing so a save takes effect
    without waiting on filesystem timestamp granularity.

    A key removed from the file is removed from ``os.environ`` too, but only
    when its current value is still the one written here -- anything set by
    other means is left alone.  Note this means deleting a key from the file
    retires it even if it originally came from the shell.

    Returns key **names** only.  Never returns, logs, or raises a value.
    """
    global _env_cache
    path = _env_file_path()
    with _env_lock:
        stamp = _env_stamp(path)
        if not force and _env_cache is not None and _env_cache[0] == stamp:
            return []
        try:
            values = read_env_file(path)
        except OSError as exc:
            # Name the path, never the contents: a traceback from the parse
            # loop can carry a line of the file, and that line is a secret.
            print(f"[mcpproxy] could not read env file {path}: {exc.strerror}", flush=True)
            return []

        changed: list[str] = []
        for key, value in values.items():
            if os.environ.get(key) != value:
                changed.append(key)
            os.environ[key] = value
            _env_injected[key] = value

        for key in [k for k in _env_injected if k not in values]:
            # Only retire what we set and nobody has overwritten since.
            if os.environ.get(key) == _env_injected[key]:
                os.environ.pop(key, None)
                changed.append(key)
            _env_injected.pop(key, None)

        _env_cache = (stamp, values)
        return sorted(changed)


def reset_env_cache() -> None:
    """Forget the cached stamp and injected-key bookkeeping (tests only)."""
    global _env_cache
    with _env_lock:
        _env_cache = None
        _env_injected.clear()

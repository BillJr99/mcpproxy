"""
HTTP frontend for mcpproxy — served on port 8889 (MCP stays on 8888).

Endpoints
---------
GET  /                          — single-page HTML UI
GET  /api/tools                 — list providers (summary)
GET  /api/tools/{name}          — get provider as structured JSON
POST /api/tools                 — create provider from structured JSON
PUT  /api/tools/{name}          — update provider from structured JSON
DELETE /api/tools/{name}        — delete provider YAML
POST /api/validate              — validate structured provider {provider}
POST /api/introspect            — spawn command, run requirements/setup, return tools list
POST /api/extract-functions     — parse Python code for async functions
GET  /api/env                   — list .env vars (values masked)
POST /api/env                   — upsert vars into .env  {vars: {KEY: VALUE}}
GET  /api/files                 — list a directory inside a mounted root (?root=&path=)
POST /api/files/mkdir           — create a directory {root, path}
POST /api/files/upload          — multipart upload (root, path, file)
GET  /api/files/download        — download a file (?root=&path=)
DELETE /api/files               — delete a file/dir (?root=&path=&recursive=)
POST /api/oauth-bootstrap       — begin a provider-declared OAuth consent flow {name}
POST /api/restart               — send SIGTERM to restart server
GET  /api/config                — UI feature flags (e.g. web_terminal)
WS   /ws/terminal               — interactive PTY terminal (optional ?cmd=…)
"""

import ast
import asyncio
import html
import fcntl
import json
import os
import pty
import re
import shlex
import shutil
import signal
import struct
import subprocess
import sys
import termios
import textwrap
import threading
import traceback
from pathlib import Path, PurePosixPath
from typing import Any

import yaml
from fastapi import (
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse

from config import CONFIG_DIR, ENV_FILE, FILES_DIR, REPOS_DIR


# ---------------------------------------------------------------------------
# Web terminal feature gate
# ---------------------------------------------------------------------------
#
# The /ws/terminal endpoint streams a real PTY to the browser so the mcp-remote
# OAuth bootstrap (and any other command) can be driven from the UI without a
# host shell or `docker exec`.  This is arbitrary command execution over HTTP —
# consistent with what the proxy already does (introspect spawns arbitrary
# commands; code providers exec() Python) and intended for a trusted,
# single-user/local admin UI.  It can be switched off with MCPPROXY_WEB_TERMINAL=0.

def _web_terminal_enabled() -> bool:
    return os.environ.get("MCPPROXY_WEB_TERMINAL", "1").strip().lower() not in (
        "0", "false", "no", "off", ""
    )


# ---------------------------------------------------------------------------
# .env helpers
# ---------------------------------------------------------------------------

def _read_env_file(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not path.exists():
        return result
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        result[key.strip()] = val.strip().strip('"').strip("'")
    return result


def _write_env_file(path: Path, updates: dict[str, str]) -> None:
    existing: dict[str, str] = {}
    lines: list[str] = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                key = stripped.partition("=")[0].strip()
                existing[key] = line
            lines.append(line)
    new_lines = list(lines)
    for key, val in updates.items():
        new_line = f"{key}={val}"
        if key in existing:
            for i, line in enumerate(new_lines):
                s = line.strip()
                if s and not s.startswith("#") and s.partition("=")[0].strip() == key:
                    new_lines[i] = new_line
                    break
        else:
            new_lines.append(new_line)
    path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


def _extract_secret_env_keys(spec: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    for tool in spec.get("tools", []):
        for key in (tool.get("secrets") or {}).get("env", {}).values():
            if key not in keys:
                keys.append(key)
    # Repository providers may declare extra env keys (auto-discovered from
    # the cloned repo's .env.example) that drive the underlying server.
    for key in (spec.get("repository") or {}).get("env_keys") or []:
        if key and key not in keys:
            keys.append(key)
    # REST providers reference auth secrets by env-var name (``*_env`` keys) in
    # the auth block, so surface those for the Secrets UI / missing-secrets badge.
    for key in _rest_auth_env_keys(spec):
        if key and key not in keys:
            keys.append(key)
    return keys


def _rest_auth_env_keys(spec: dict[str, Any]) -> list[str]:
    """Return the env-var names referenced by a REST provider's auth block."""
    auth = (spec.get("rest") or {}).get("auth") or {}
    candidates = ("token_env", "value_env", "client_id_env", "client_secret_env")
    return [auth[k] for k in candidates if auth.get(k)]


_ENV_EXAMPLE_CANDIDATES = (".env.example", ".env.sample", ".env.template")


def _write_workdir_env_file(workdir: str | Path, env_keys: list[str]) -> Path:
    """Write a ``.env`` file inside ``workdir`` populated from ``os.environ``.

    Only keys with a non-empty value in the current process environment are
    written.  This lets dotenv-style loaders inside the cloned repo (such as
    ``tsx --env-file=.env``) pick up secrets supplied via the proxy's
    Secrets UI without leaking unset placeholders.  Returns the written path.
    """
    target = Path(workdir) / ".env"
    target.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for key in env_keys:
        val = os.environ.get(key)
        if val:
            lines.append(f"{key}={val}")
    target.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return target


def _parse_env_example(workdir: str | Path) -> list[str]:
    """Return the ordered list of KEY names from the first .env.example-style
    file found in ``workdir``.  Returns ``[]`` when no candidate exists.
    """
    wd = Path(workdir)
    for name in _ENV_EXAMPLE_CANDIDATES:
        candidate = wd / name
        if candidate.exists():
            return list(_read_env_file(candidate).keys())
    return []


# ---------------------------------------------------------------------------
# Package manager detection (for logging / display — execution is identical)
# ---------------------------------------------------------------------------

def _detect_package_manager(command: str) -> str:
    """Identify the package manager from the first token of a command string."""
    first = command.strip().split()[0] if command.strip() else ""
    if first == "npx":
        return "npx"
    if first == "uvx":
        return "uvx"
    if first in ("python", "python3"):
        return "pip"
    if first == "npm":
        return "npm"
    return "command"


# ---------------------------------------------------------------------------
# Structured ↔ YAML conversion
# ---------------------------------------------------------------------------

def _get_package_spec(spec: dict[str, Any]) -> dict[str, Any] | None:
    """Return the subprocess sub-dict (package:), or None for code providers."""
    return spec.get("package") or None


def _get_repository_spec(spec: dict[str, Any]) -> dict[str, Any] | None:
    """Return the repository sub-dict (repository:), or None when absent."""
    return spec.get("repository") or None


def _get_rest_spec(spec: dict[str, Any]) -> dict[str, Any] | None:
    """Return the rest sub-dict (rest:), or None for non-REST providers."""
    return spec.get("rest") or None


def _safe_provider_dirname(name: str) -> str:
    """Normalize a provider name into a safe single-segment directory name."""
    safe = re.sub(r"[^a-zA-Z0-9_-]", "-", name or "").strip("-")
    return safe or "repo"


def _repository_workdir(name: str, explicit: str | None = None) -> str:
    """Resolve the on-disk workdir path for a repository provider."""
    explicit = (explicit or "").strip()
    if explicit:
        return explicit
    return str(REPOS_DIR / _safe_provider_dirname(name))


def _provider_to_structured(name: str, spec: dict[str, Any]) -> dict[str, Any]:
    """Convert a loaded YAML spec into the structured JSON the UI works with."""
    tools_out = []
    for t in spec.get("tools", []):
        schema = t.get("input_schema", {}) or {}
        props = schema.get("properties", {}) or {}
        required = set(schema.get("required", []) or [])
        params = []
        for pname, pdef in props.items():
            params.append({
                "name": pname,
                "type": pdef.get("type", "string"),
                "description": pdef.get("description", ""),
                "required": pname in required,
                "default": pdef.get("default"),
            })
        secrets = []
        for arg, env in ((t.get("secrets") or {}).get("env", {}) or {}).items():
            secrets.append({"arg": arg, "env": env})
        tools_out.append({
            "name": t.get("name", ""),
            "function": t.get("function", ""),
            "description": t.get("description", ""),
            "documentation": t.get("documentation", ""),
            "enabled": False if t.get("enabled") is False else True,
            "parameters": params,
            "secrets": secrets,
        })

    pkg_sub = _get_package_spec(spec)
    repo_sub = _get_repository_spec(spec)
    rest_sub = _get_rest_spec(spec)
    rest_out: dict[str, Any] = {}
    if rest_sub is not None:
        ptype = "rest"
        command = ""
        repo_url = ""
        repo_ref = ""
        build_commands = []
        workdir = ""
        repo_env_keys = []
        rest_out = {
            "base_url": (rest_sub.get("base_url") or "").strip(),
            "headers": dict(rest_sub.get("headers") or {}),
            "auth": dict(rest_sub.get("auth") or {"type": "none"}),
            "openapi": (rest_sub.get("openapi") or "").strip(),
            "endpoints": list(rest_sub.get("endpoints") or []),
        }
    elif repo_sub is not None:
        ptype = "repository"
        command = (pkg_sub.get("command") if pkg_sub else "") or ""
        command = command.strip()
        repo_url = (repo_sub.get("url") or "").strip()
        repo_ref = (repo_sub.get("ref") or "").strip()
        build_commands = list(repo_sub.get("build_commands") or [])
        workdir = _repository_workdir(name, repo_sub.get("workdir"))
        repo_env_keys = list(repo_sub.get("env_keys") or [])
    elif pkg_sub is not None:
        ptype = "package"
        command = (pkg_sub.get("command") or "").strip()
        repo_url = ""
        repo_ref = ""
        build_commands = []
        workdir = ""
        repo_env_keys = []
    else:
        ptype = "code"
        command = ""
        repo_url = ""
        repo_ref = ""
        build_commands = []
        workdir = ""
        repo_env_keys = []

    return {
        "name": name,
        "documentation": spec.get("documentation", ""),
        "type": ptype,
        "command": command,
        "code": spec.get("code", ""),
        "requirements": list(spec.get("requirements") or []),
        "setup_commands": list(spec.get("setup_commands") or []),
        "repo_url": repo_url,
        "repo_ref": repo_ref,
        "build_commands": build_commands,
        "repo_env_keys": repo_env_keys,
        "workdir": workdir,
        "rest": rest_out,
        "oauth": dict(spec.get("oauth") or {}),
        "tools": tools_out,
    }


def _structured_to_yaml(provider: dict[str, Any]) -> str:
    """Convert the structured JSON provider dict back to a YAML string."""
    spec: dict[str, Any] = {}

    doc = (provider.get("documentation") or "").strip()
    if doc:
        spec["documentation"] = doc + "\n"

    ptype = provider.get("type", "code")

    if ptype == "rest":
        rest_in = provider.get("rest") or {}
        rest_block: dict[str, Any] = {
            "base_url": (rest_in.get("base_url") or "").strip(),
        }
        headers = {k: v for k, v in (rest_in.get("headers") or {}).items() if k}
        if headers:
            rest_block["headers"] = headers
        auth = dict(rest_in.get("auth") or {"type": "none"})
        auth.setdefault("type", "none")
        rest_block["auth"] = auth
        openapi = (rest_in.get("openapi") or "").strip()
        if openapi:
            rest_block["openapi"] = openapi
        endpoints = [e for e in (rest_in.get("endpoints") or []) if e.get("name")]
        if endpoints:
            rest_block["endpoints"] = endpoints
        spec["rest"] = rest_block
    elif ptype == "package":
        spec["package"] = {"command": (provider.get("command") or "").strip()}
    elif ptype == "repository":
        spec["package"] = {"command": (provider.get("command") or "").strip()}
        repo_block: dict[str, Any] = {
            "url": (provider.get("repo_url") or "").strip(),
        }
        ref = (provider.get("repo_ref") or "").strip()
        if ref:
            repo_block["ref"] = ref
        workdir = (provider.get("workdir") or "").strip()
        if workdir:
            repo_block["workdir"] = workdir
        build_commands = [c for c in (provider.get("build_commands") or []) if c]
        if build_commands:
            repo_block["build_commands"] = build_commands
        env_keys = [k for k in (provider.get("repo_env_keys") or []) if k]
        if env_keys:
            repo_block["env_keys"] = env_keys
        spec["repository"] = repo_block
    else:
        code = (provider.get("code") or "").strip()
        if code:
            spec["code"] = code + "\n"

    oauth = provider.get("oauth") or {}
    if oauth.get("type"):
        oauth_block: dict[str, Any] = {"type": oauth["type"]}
        for key in ("client_secret_file", "token_file"):
            if (oauth.get(key) or "").strip():
                oauth_block[key] = oauth[key].strip()
        scopes = [s for s in (oauth.get("scopes") or []) if s]
        if scopes:
            oauth_block["scopes"] = scopes
        for key in ("prompt", "login_hint"):
            if oauth.get(key):
                oauth_block[key] = oauth[key]
        spec["oauth"] = oauth_block

    requirements = [r for r in (provider.get("requirements") or []) if r]
    if requirements:
        spec["requirements"] = requirements

    setup_commands = [c for c in (provider.get("setup_commands") or []) if c]
    if setup_commands:
        spec["setup_commands"] = setup_commands

    tools_out = []
    for t in provider.get("tools", []):
        props: dict[str, Any] = {}
        required: list[str] = []
        for p in t.get("parameters", []):
            pname = p["name"]
            pdef: dict[str, Any] = {"type": p.get("type", "string")}
            if p.get("description"):
                pdef["description"] = p["description"]
            if p.get("default") is not None:
                pdef["default"] = p["default"]
            props[pname] = pdef
            if p.get("required"):
                required.append(pname)

        tool_entry: dict[str, Any] = {
            "name": t["name"],
            "description": t.get("description", ""),
            "enabled": False if t.get("enabled") is False else True,
            "input_schema": {"type": "object", "properties": props, "required": required},
        }
        if ptype == "code":
            tool_entry["function"] = t.get("function") or t["name"]
        tdoc = (t.get("documentation") or "").strip()
        if tdoc:
            tool_entry["documentation"] = tdoc
        secrets = t.get("secrets", [])
        if secrets:
            tool_entry["secrets"] = {"env": {s["arg"]: s["env"] for s in secrets}}
        tools_out.append(tool_entry)

    spec["tools"] = tools_out
    return yaml.dump(spec, default_flow_style=False, allow_unicode=True, sort_keys=False)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

_REST_AUTH_TYPES = {"none", "bearer", "api_key", "client_credentials", "authorization_code"}


def _validate_rest(provider: dict[str, Any]) -> list[str]:
    """Return validation errors for a REST provider's ``rest`` block."""
    errors: list[str] = []
    rest = provider.get("rest") or {}
    if not (rest.get("base_url") or "").strip():
        errors.append("base_url is required for REST providers")

    auth = rest.get("auth") or {}
    atype = (auth.get("type") or "none").strip()
    if atype not in _REST_AUTH_TYPES:
        errors.append(f"auth.type must be one of {sorted(_REST_AUTH_TYPES)}")
    if atype == "bearer" and not (auth.get("token_env") or "").strip():
        errors.append("auth.token_env is required for bearer auth")
    if atype == "api_key" and not (auth.get("value_env") or "").strip():
        errors.append("auth.value_env is required for api_key auth")
    if atype == "client_credentials":
        for key in ("token_url", "client_id_env", "client_secret_env"):
            if not (auth.get(key) or "").strip():
                errors.append(f"auth.{key} is required for client_credentials auth")
    if atype == "authorization_code":
        for key in ("authorize_url", "token_url", "client_id_env"):
            if not (auth.get(key) or "").strip():
                errors.append(f"auth.{key} is required for authorization_code auth")

    openapi = (rest.get("openapi") or "").strip()
    endpoints = rest.get("endpoints") or []
    if not openapi and not endpoints:
        errors.append("REST providers need either an openapi source or at least one endpoint")
    for i, ep in enumerate(endpoints):
        if not (ep.get("method") or "").strip():
            errors.append(f"rest.endpoints[{i}]: method is required")
        if not (ep.get("path") or "").strip():
            errors.append(f"rest.endpoints[{i}]: path is required")
    return errors


def _validate_oauth(provider: dict[str, Any]) -> list[str]:
    """Return validation errors for a provider's top-level ``oauth`` block."""
    import oauth_bootstrap

    oauth = provider.get("oauth") or {}
    if not oauth.get("type"):
        return []
    errors: list[str] = []
    otype = (oauth.get("type") or "").strip()
    if otype not in oauth_bootstrap.SUPPORTED_TYPES:
        errors.append(
            f"oauth.type must be one of {sorted(oauth_bootstrap.SUPPORTED_TYPES)}"
        )
        return errors
    for key in ("client_secret_file", "token_file"):
        if not (oauth.get(key) or "").strip():
            errors.append(f"oauth.{key} is required for {otype} oauth")
    scopes = oauth.get("scopes")
    if not isinstance(scopes, list) or not [s for s in (scopes or []) if s]:
        errors.append("oauth.scopes must be a non-empty list")
    secret_file = (oauth.get("client_secret_file") or "").strip()
    if secret_file and not Path(secret_file).is_file():
        errors.append(
            f"oauth.client_secret_file not found: {secret_file} — upload it via "
            "the Files manager (e.g. tools/secrets/)"
        )
    return errors


def _validate_provider(provider: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    ptype = provider.get("type", "code")
    errors.extend(_validate_oauth(provider))

    if ptype == "rest":
        errors.extend(_validate_rest(provider))
    elif ptype == "package":
        if not (provider.get("command") or "").strip():
            errors.append("command is required for package providers")
    elif ptype == "repository":
        if not (provider.get("repo_url") or "").strip():
            errors.append("repo_url is required for repository providers")
        if not (provider.get("command") or "").strip():
            errors.append("command is required for repository providers")
        build_commands = provider.get("build_commands")
        if build_commands is not None and not isinstance(build_commands, list):
            errors.append("build_commands must be a list")
    else:
        if not (provider.get("code") or "").strip():
            errors.append("code is required for code providers")

    requirements = provider.get("requirements")
    if requirements is not None and not isinstance(requirements, list):
        errors.append("requirements must be a list")

    setup_commands = provider.get("setup_commands")
    if setup_commands is not None and not isinstance(setup_commands, list):
        errors.append("setup_commands must be a list")

    tools = provider.get("tools", [])
    if not tools:
        errors.append("At least one tool is required")
    for i, t in enumerate(tools):
        if not (t.get("name") or "").strip():
            errors.append(f"tools[{i}]: name is required")
        if not (t.get("description") or "").strip():
            errors.append(f"tools[{i}]: description is required")
        if ptype == "code" and not (t.get("function") or "").strip():
            errors.append(f"tools[{i}]: function is required for code providers")
        for j, p in enumerate(t.get("parameters", [])):
            if not (p.get("name") or "").strip():
                errors.append(f"tools[{i}].parameters[{j}]: name is required")

    return {"ok": len(errors) == 0, "errors": errors}


def _validate_or_raise(provider: dict[str, Any]) -> None:
    result = _validate_provider(provider)
    if not result["ok"]:
        raise HTTPException(400, "; ".join(result["errors"]))


def _guard_name(name: str) -> None:
    if not name:
        raise HTTPException(400, "name is required")
    if not re.match(r'^[a-zA-Z][a-zA-Z0-9_\-]*$', name):
        raise HTTPException(400, "name must start with a letter and contain only letters, digits, _ or -")


# ---------------------------------------------------------------------------
# Code analysis helper (for code provider wizard)
# ---------------------------------------------------------------------------

def _extract_functions(code: str) -> dict[str, Any]:
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return {"ok": False, "error": str(exc), "functions": []}
    functions = []
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef):
            arg_names = [a.arg for a in node.args.args]
            if "context" not in arg_names:
                continue
            params = [
                {"name": a.arg, "type": "string"}
                for a in node.args.args if a.arg != "context"
            ]
            functions.append({"name": node.name, "params": params})
    return {"ok": True, "functions": functions}


# ---------------------------------------------------------------------------
# FastAPI app factory
# ---------------------------------------------------------------------------

def _safe_local_openapi_path(source: str) -> str:
    """Resolve a local OpenAPI file path, restricted to FILES_DIR.

    Stops the introspection endpoint from being used to read arbitrary files
    (e.g. ``/app/.env``).  Returns the resolved absolute path, or raises
    ``ValueError`` if the path escapes the files directory or does not exist.
    """
    base = FILES_DIR.resolve()
    candidate = Path(source)
    candidate = candidate.resolve() if candidate.is_absolute() else (base / candidate).resolve()
    if candidate != base and base not in candidate.parents:
        raise ValueError(
            f"Local OpenAPI files must live under the files directory ({base}). "
            "Use an http(s) URL, or place the spec in that directory."
        )
    if not candidate.is_file():
        raise ValueError(f"OpenAPI file not found in the files directory: {source}")
    return str(candidate)


# ---------------------------------------------------------------------------
# File manager helpers
# ---------------------------------------------------------------------------
#
# The /api/files endpoints let the UI manage the volume-mounted directories
# (tools, files, repos) — e.g. create /app/tools/secrets and upload
# client_secret.json into it.  Like the rest of the UI there is no auth: this
# is intended for a trusted, single-user/local admin UI.

MAX_UPLOAD_BYTES = int(os.environ.get("MCPPROXY_MAX_UPLOAD_BYTES", 50 * 1024 * 1024))


def _resolve_in_root(roots: dict[str, Path], root: str, rel: str) -> Path:
    """Resolve ``rel`` inside the whitelisted root, rejecting escapes.

    Resolves symlinks before checking containment, so both ``..`` traversal
    and symlinks pointing outside the root raise HTTPException(400).
    """
    base = roots.get(root)
    if base is None:
        raise HTTPException(400, f"Unknown root {root!r} (expected one of {sorted(roots)})")
    base = base.resolve()
    target = (base / (rel or "").lstrip("/")).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        raise HTTPException(400, f"Path escapes the {root} directory: {rel!r}")
    return target


def create_app(
    config_dir: Path | None = None,
    env_file: Path | None = None,
    file_roots: dict[str, Path] | None = None,
) -> "FastAPI":
    _config_dir = config_dir or CONFIG_DIR
    _env_file = env_file or ENV_FILE
    _file_roots = file_roots or {
        "tools": _config_dir,
        "files": FILES_DIR,
        "repos": REPOS_DIR,
    }

    app = FastAPI(title="mcpproxy UI", docs_url=None, redoc_url=None)

    # ── Provider CRUD ────────────────────────────────────────────────────────

    @app.get("/api/tools")
    async def list_tools() -> list[dict]:
        out: list[dict] = []
        if not _config_dir.exists():
            return out
        env_vars = _read_env_file(_env_file)
        for path in sorted(_config_dir.glob("*.yaml")):
            try:
                spec = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                tool_entries = spec.get("tools") or []
                secret_keys = _extract_secret_env_keys(spec)
                missing_secrets = [k for k in secret_keys if not env_vars.get(k)]
                structured = _provider_to_structured(path.stem, spec)
                validation = _validate_provider(structured)
                is_package = bool(_get_package_spec(spec))
                is_repository = bool(_get_repository_spec(spec))
                is_rest = bool(_get_rest_spec(spec))
                oauth_cfg = spec.get("oauth") or {}
                oauth_out = None
                if oauth_cfg.get("type"):
                    import oauth_bootstrap
                    oauth_out = {"type": oauth_cfg.get("type"),
                                 **oauth_bootstrap.token_status(oauth_cfg)}
                out.append({
                    "name": path.stem,
                    "file": path.name,
                    "tool_count": len(tool_entries),
                    "tool_names": [t.get("name") for t in tool_entries],
                    "provider_type": structured["type"],
                    "is_package": is_package,
                    "is_repository": is_repository,
                    "is_rest": is_rest,
                    "secret_keys": secret_keys,
                    "missing_secrets": missing_secrets,
                    "validation_errors": validation["errors"],
                    "documentation": spec.get("documentation") or "",
                    "oauth": oauth_out,
                })
            except Exception as exc:
                out.append({"name": path.stem, "file": path.name, "error": str(exc)})
        return out

    @app.get("/api/tools/{name}")
    async def get_tool(name: str) -> dict:
        _guard_name(name)
        path = _config_dir / f"{name}.yaml"
        if not path.exists():
            raise HTTPException(404, f"Provider '{name}' not found")
        spec = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return _provider_to_structured(name, spec)

    @app.post("/api/tools")
    async def create_tool(request: Request) -> dict:
        body = await request.json()
        name = (body.get("name") or "").strip()
        _guard_name(name)
        path = _config_dir / f"{name}.yaml"
        if path.exists():
            raise HTTPException(409, f"Provider '{name}' already exists")
        provider = body.get("provider") or body  # accept whole body as provider
        provider["name"] = name
        _validate_or_raise(provider)
        content = _structured_to_yaml(provider)
        _config_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        spec = yaml.safe_load(content) or {}
        return {"ok": True, "name": name, "secret_keys": _extract_secret_env_keys(spec)}

    @app.put("/api/tools/{name}")
    async def update_tool(name: str, request: Request) -> dict:
        _guard_name(name)
        body = await request.json()
        provider = body.get("provider") or body
        provider["name"] = name
        _validate_or_raise(provider)
        content = _structured_to_yaml(provider)
        path = _config_dir / f"{name}.yaml"
        path.write_text(content, encoding="utf-8")
        spec = yaml.safe_load(content) or {}
        return {"ok": True, "secret_keys": _extract_secret_env_keys(spec)}

    @app.delete("/api/tools/{name}")
    async def delete_tool(name: str) -> dict:
        _guard_name(name)
        path = _config_dir / f"{name}.yaml"
        if not path.exists():
            raise HTTPException(404, f"Provider '{name}' not found")
        path.unlink()
        return {"ok": True}

    # ── Validation ───────────────────────────────────────────────────────────

    @app.post("/api/validate")
    async def validate_provider(request: Request) -> dict:
        body = await request.json()
        provider = body.get("provider") or body
        return _validate_provider(provider)

    # ── Package introspection ─────────────────────────────────────────────────

    @app.post("/api/introspect")
    async def introspect_package(request: Request) -> dict:
        """Introspect any stdio MCP server command.

        Accepts:
          command        — the command to run (required)
          requirements   — list of pip packages to install first (optional)
          setup_commands — list of shell commands to run before spawning (optional)

        Auto-detects the package manager from the command prefix (npx, uvx,
        python, etc.) for logging purposes; execution is identical for all.
        """
        body = await request.json()
        command = (body.get("command") or "").strip()
        requirements: list[str] = body.get("requirements") or []
        setup_commands: list[str] = body.get("setup_commands") or []
        cwd = (body.get("cwd") or "").strip() or None
        env_keys = list(body.get("env_keys") or []) or None
        if not command:
            raise HTTPException(400, "command is required")

        pm = _detect_package_manager(command)

        try:
            # 1. Install pip requirements
            for req in requirements:
                if not req:
                    continue
                subprocess.run(
                    [sys.executable, "-m", "pip", "install", req],
                    check=True,
                )

            # 2. Run setup commands (in cwd when one is supplied — e.g. a repo workdir)
            for cmd in setup_commands:
                if not cmd:
                    continue
                subprocess.run(shlex.split(cmd), check=True, cwd=cwd)

            # 3. Introspect the MCP server
            from process_runner import introspect
            tools = await introspect(command, cwd=cwd, env_keys=env_keys)
            return {"ok": True, "tools": tools, "package_manager": pm}
        except Exception as exc:
            traceback.print_exc()
            return {"ok": False, "error": str(exc), "tools": [], "package_manager": pm}

    @app.get("/api/pending-auth")
    async def pending_auth(command: str = "") -> dict:
        """Return the OAuth authorization URL a spawn is currently waiting on.

        Remote OAuth-protected MCP servers reached via the `mcp-remote` bridge
        print an authorization URL to stderr and block the MCP handshake until
        the user authorizes in a browser.  `process_runner` scrapes that URL;
        the wizard polls this endpoint (while an introspect call is blocked) to
        show a clickable "Authorize" link.  Once a valid token cache exists the
        bridge refreshes silently and this stays empty.

        With no `command`, returns every pending URL keyed by spawn command.
        REST providers' authorization_code flows publish their URLs the same way
        (keyed by provider name) and are merged into the ``pending`` map.
        """
        from process_runner import pending_auth_urls
        from rest_provider import pending_rest_auth
        if command:
            return {"ok": True, "auth_url": pending_auth_urls.get(command.strip())}
        merged = {**pending_auth_urls, **pending_rest_auth}
        return {"ok": True, "pending": merged, "rest_pending": dict(pending_rest_auth)}

    # ── REST / OpenAPI ───────────────────────────────────────────────────────

    @app.post("/api/introspect-openapi")
    async def introspect_openapi_endpoint(request: Request) -> dict:
        """Parse an OpenAPI 3.x / Swagger 2.0 spec (URL or file) into endpoints + tools.

        Body: { openapi: <url-or-path> }.  Returns ``{ok, endpoints, tools}`` (or
        ``{ok: False, error}``).  The wizard calls this to expand an OpenAPI source
        into concrete endpoints before saving, so the server never fetches the spec
        at registration time.

        Local file sources are restricted to the files directory (FILES_DIR) so the
        endpoint can't read arbitrary files (e.g. ``.env``).  Parsing performs
        blocking network/file I/O, so it runs in a worker thread to avoid blocking
        the UI event loop.
        """
        body = await request.json()
        source = (body.get("openapi") or "").strip()
        if not source:
            raise HTTPException(400, "openapi (URL or file path) is required")
        if not (source.startswith("http://") or source.startswith("https://")):
            try:
                source = _safe_local_openapi_path(source)
            except ValueError as exc:
                return {"ok": False, "error": str(exc), "endpoints": [], "tools": []}
        try:
            from rest_provider import introspect_openapi
            endpoints, tools = await asyncio.to_thread(introspect_openapi, source)
            return {"ok": True, "endpoints": endpoints, "tools": tools}
        except Exception as exc:
            traceback.print_exc()
            return {"ok": False, "error": str(exc), "endpoints": [], "tools": []}

    @app.get("/api/catalog")
    async def get_catalog(live: bool = False, source: str | None = None) -> dict:
        """Browseable provider catalog — curated entries plus optional live registries.

        ``?live=true`` enables probing external registries (MCP registry,
        Smithery, APIs.guru); ``?source=mcp_registry,apis_guru`` narrows which
        ones.  The endpoint takes no caller-supplied URL — it only ever hits a
        fixed allowlist of registry hosts — and always returns the curated list
        even if every live source errors (failures land in ``errors``).
        """
        import catalog
        sources = [s.strip() for s in source.split(",") if s.strip()] if source else None
        return await catalog.build_catalog(live=live, sources=sources)

    @app.post("/api/rest-authorize")
    async def rest_authorize(request: Request) -> dict:
        """Begin an authorization_code flow for a saved REST provider.

        Body: { name: <provider> }.  Reads the provider's auth block from its
        YAML, builds the PKCE authorize URL, publishes it to ``pending_rest_auth``,
        and returns ``{ok, auth_url, redirect_uri}`` for the UI to open.
        """
        body = await request.json()
        name = (body.get("name") or "").strip()
        _guard_name(name)
        path = _config_dir / f"{name}.yaml"
        if not path.exists():
            raise HTTPException(404, f"Provider '{name}' not found")
        spec = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        auth = (spec.get("rest") or {}).get("auth") or {}
        if (auth.get("type") or "").strip() != "authorization_code":
            raise HTTPException(400, "Provider does not use authorization_code auth")
        try:
            from rest_provider import AuthCodeTokenStore, oauth_redirect_uri
            store = AuthCodeTokenStore(name, auth)
            auth_url = store.begin_authorization()
            return {"ok": True, "auth_url": auth_url, "redirect_uri": oauth_redirect_uri()}
        except Exception as exc:
            traceback.print_exc()
            return {"ok": False, "error": str(exc)}

    @app.post("/api/oauth-bootstrap")
    async def oauth_bootstrap_endpoint(request: Request) -> dict:
        """Begin (or restart) the consent flow declared by a provider's
        top-level ``oauth:`` block (e.g. a Google token-file bootstrap).

        Body: { name: <provider> }.  Builds the consent URL (offline access +
        PKCE), publishes it to the pending-auth banner, and returns
        ``{ok, auth_url, redirect_uri, token}`` for the UI to open.
        """
        body = await request.json()
        name = (body.get("name") or "").strip()
        _guard_name(name)
        path = _config_dir / f"{name}.yaml"
        if not path.exists():
            raise HTTPException(404, f"Provider '{name}' not found")
        spec = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        import oauth_bootstrap
        oauth_cfg = oauth_bootstrap.get_oauth_config(spec)
        if oauth_cfg is None:
            raise HTTPException(400, "Provider has no oauth block")
        if (oauth_cfg.get("type") or "").strip() not in oauth_bootstrap.SUPPORTED_TYPES:
            raise HTTPException(
                400,
                f"Unsupported oauth.type (supported: {sorted(oauth_bootstrap.SUPPORTED_TYPES)})",
            )
        try:
            from rest_provider import oauth_redirect_uri
            auth_url = oauth_bootstrap.begin_authorization(name, oauth_cfg)
            return {
                "ok": True,
                "auth_url": auth_url,
                "redirect_uri": oauth_redirect_uri(),
                "token": oauth_bootstrap.token_status(oauth_cfg),
            }
        except Exception as exc:
            traceback.print_exc()
            return {"ok": False, "error": str(exc)}

    @app.get("/oauth/callback")
    async def oauth_callback(
        code: str = "", state: str = "", error: str = ""
    ) -> "HTMLResponse":
        """OAuth redirect target for REST providers' authorization_code flow.

        Exchanges ``code`` for tokens (using the PKCE verifier registered under
        ``state``) and persists them, then renders a small close-the-tab page.
        """
        # Escape every interpolated value: these come from the redirect query
        # string / upstream errors and must not be able to inject HTML or script.
        if error:
            return HTMLResponse(
                f"<h3>Authorization failed</h3><p>{html.escape(error)}</p>", status_code=400
            )
        if not code or not state:
            return HTMLResponse("<h3>Missing code or state</h3>", status_code=400)
        try:
            from rest_provider import AuthCodeTokenStore
            await AuthCodeTokenStore.complete_authorization(state, code)
            return HTMLResponse(
                "<h3>Authorization complete</h3>"
                "<p>You may close this tab and return to mcpproxy.</p>"
            )
        except Exception as exc:
            traceback.print_exc()
            return HTMLResponse(
                f"<h3>Authorization error</h3><p>{html.escape(str(exc))}</p>", status_code=400
            )

    # ── Repository clone-and-build ───────────────────────────────────────────

    @app.post("/api/clone-and-build")
    async def clone_and_build(request: Request) -> dict:
        """Clone (or pull) a git repo and run build_commands inside the workdir.

        Body:
          name           — provider name (used to derive the default workdir)
          repo_url       — git URL (required)
          ref            — optional branch/tag/commit
          build_commands — optional list of shell commands run inside the workdir
          workdir        — optional explicit workdir path (overrides default)

        Idempotent: if ``<workdir>/.git`` exists, runs ``git pull`` instead of
        ``git clone`` so persistent volumes pick up upstream changes.
        """
        body = await request.json()
        name = (body.get("name") or "").strip()
        url = (body.get("repo_url") or "").strip()
        ref = (body.get("ref") or "").strip()
        explicit_workdir = (body.get("workdir") or "").strip()
        build_commands: list[str] = body.get("build_commands") or []
        if not name:
            raise HTTPException(400, "name is required")
        _guard_name(name)
        if not url:
            raise HTTPException(400, "repo_url is required")

        workdir = _repository_workdir(name, explicit_workdir)

        # The clone step is required — if that fails we have nothing useful
        # to return.  Build-command failures are tolerated: they're often
        # caused by missing .env values, and the user needs the discovered
        # env_keys back so they can populate secrets and retry on next
        # restart (when materialize_repository writes .env first).
        try:
            Path(workdir).parent.mkdir(parents=True, exist_ok=True)
            if (Path(workdir) / ".git").exists():
                subprocess.run(["git", "-C", workdir, "pull", "--ff-only"], check=True)
            else:
                subprocess.run(["git", "clone", url, workdir], check=True)
            if ref:
                subprocess.run(["git", "-C", workdir, "checkout", ref], check=True)
        except Exception as exc:
            traceback.print_exc()
            return {
                "ok": False,
                "error": str(exc),
                "workdir": workdir,
                "env_keys": [],
            }

        # Parse .env.example BEFORE running build commands so the user
        # gets the secret list back even if a build command fails.
        env_keys = _parse_env_example(workdir)

        # Write a best-effort .env from currently-set environment values so
        # build commands that already invoke dotenv loaders (e.g.
        # `tsx --env-file=.env`) succeed when secrets are present.
        if env_keys:
            try:
                _write_workdir_env_file(workdir, env_keys)
            except Exception:
                traceback.print_exc()

        for cmd in build_commands:
            if not cmd:
                continue
            try:
                subprocess.run(shlex.split(cmd), check=True, cwd=workdir)
            except Exception as exc:
                traceback.print_exc()
                return {
                    "ok": False,
                    "error": str(exc),
                    "failed_command": cmd,
                    "workdir": workdir,
                    "env_keys": env_keys,
                }

        return {"ok": True, "workdir": workdir, "env_keys": env_keys}

    @app.post("/api/scan-env-example")
    async def scan_env_example(request: Request) -> dict:
        """Re-scan ``<workdir>/.env.example`` (or sibling) and return KEY names."""
        body = await request.json()
        workdir = (body.get("workdir") or "").strip()
        if not workdir:
            raise HTTPException(400, "workdir is required")
        try:
            return {"ok": True, "env_keys": _parse_env_example(workdir)}
        except Exception as exc:
            traceback.print_exc()
            return {"ok": False, "error": str(exc), "env_keys": []}

    # ── Function extractor (code providers) ──────────────────────────────────

    @app.post("/api/extract-functions")
    async def extract_functions(request: Request) -> dict:
        body = await request.json()
        code = body.get("code") or ""
        return _extract_functions(code)

    # ── .env management ──────────────────────────────────────────────────────

    @app.get("/api/env")
    async def get_env() -> dict:
        current = _read_env_file(_env_file)
        return {
            "vars": {k: ("***" if v else "") for k, v in current.items()},
            "env_file": str(_env_file),
        }

    @app.post("/api/env")
    async def set_env(request: Request) -> dict:
        body = await request.json()
        updates: dict[str, str] = body.get("vars") or {}
        if not isinstance(updates, dict):
            raise HTTPException(400, "'vars' must be an object")
        for k in updates:
            if not re.match(r'^[A-Z][A-Z0-9_]*$', k):
                raise HTTPException(400, f"Invalid env var name: {k!r}")
        _write_env_file(_env_file, updates)
        return {"ok": True, "written": list(updates.keys())}

    # ── File manager ─────────────────────────────────────────────────────────
    #
    # Browse / mkdir / upload / download / delete inside the volume-mounted
    # roots (tools, files, repos).  Paths are always relative to a whitelisted
    # root and validated with resolve()+relative_to() — see _resolve_in_root.

    @app.get("/api/files")
    async def list_dir(root: str = "tools", path: str = "") -> dict:
        target = _resolve_in_root(_file_roots, root, path)
        entries: list[dict[str, Any]] = []
        if target.is_dir():
            base = _file_roots[root].resolve()
            for entry in target.iterdir():
                if entry.is_symlink():
                    etype = "symlink"
                elif entry.is_dir():
                    etype = "directory"
                else:
                    etype = "file"
                try:
                    stat = entry.stat() if etype != "symlink" else entry.lstat()
                    size = stat.st_size if etype == "file" else 0
                    mtime = stat.st_mtime
                except OSError:
                    size, mtime = 0, 0
                entries.append({
                    "name": entry.name,
                    "path": entry.relative_to(base).as_posix(),
                    "type": etype,
                    "size": size,
                    "mtime": mtime,
                })
            entries.sort(key=lambda e: (e["type"] != "directory", e["name"].lower()))
        return {
            "ok": True,
            "root": root,
            "path": path,
            "roots": sorted(_file_roots),
            "entries": entries,
        }

    @app.post("/api/files/mkdir")
    async def make_dir(request: Request) -> dict:
        body = await request.json()
        root = (body.get("root") or "tools").strip()
        rel = (body.get("path") or "").strip()
        if not rel.strip("/"):
            raise HTTPException(400, "path is required")
        target = _resolve_in_root(_file_roots, root, rel)
        if target.exists() and not target.is_dir():
            raise HTTPException(400, f"A file already exists at {rel!r}")
        target.mkdir(parents=True, exist_ok=True)
        return {"ok": True, "path": target.relative_to(_file_roots[root].resolve()).as_posix()}

    @app.post("/api/files/upload")
    async def upload_file(
        root: str = Form("tools"),
        path: str = Form(""),
        file: UploadFile = File(...),
    ) -> dict:
        """Upload a file into ``path`` (a directory) under the given root.

        The client-supplied filename is reduced to its basename, so it cannot
        carry path segments.  Existing files are overwritten — this is a
        trusted single-admin UI and re-uploading a corrected file is the
        common case.
        """
        name = Path(file.filename or "").name
        if not name or name in (".", ".."):
            raise HTTPException(400, "Invalid filename")
        target_dir = _resolve_in_root(_file_roots, root, path)
        if target_dir.exists() and not target_dir.is_dir():
            raise HTTPException(400, f"Upload target {path!r} is not a directory")
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / name
        size = 0
        try:
            with target.open("wb") as out:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_UPLOAD_BYTES:
                        raise HTTPException(
                            413, f"File exceeds the upload limit ({MAX_UPLOAD_BYTES} bytes)"
                        )
                    out.write(chunk)
        except HTTPException:
            target.unlink(missing_ok=True)
            raise
        rel = target.relative_to(_file_roots[root].resolve()).as_posix()
        return {"ok": True, "path": rel, "size": size}

    @app.get("/api/files/download")
    async def download_file(root: str = "tools", path: str = "") -> FileResponse:
        target = _resolve_in_root(_file_roots, root, path)
        if not target.is_file():
            raise HTTPException(404, f"File not found: {path!r}")
        return FileResponse(target, filename=target.name)

    @app.delete("/api/files")
    async def delete_path(root: str = "tools", path: str = "", recursive: bool = False) -> dict:
        rel = PurePosixPath(path.strip("/"))
        if not rel.name or rel.name in (".", ".."):
            raise HTTPException(400, "path is required (cannot delete the root itself)")
        # Validate the parent directory, then lstat the final component: a
        # symlink pointing outside the root would fail _resolve_in_root, but
        # deleting the link itself is safe — remove it without following it.
        parent = _resolve_in_root(_file_roots, root, rel.parent.as_posix())
        target = parent / rel.name
        if target.is_symlink():
            target.unlink()
            return {"ok": True}
        if not target.exists():
            raise HTTPException(404, f"Not found: {path!r}")
        if target.is_dir():
            if recursive:
                shutil.rmtree(target)
            else:
                try:
                    target.rmdir()
                except OSError:
                    raise HTTPException(
                        400, f"Directory {path!r} is not empty (pass recursive=true)"
                    )
        else:
            target.unlink()
        return {"ok": True}

    # ── OpenAI-compatible tool endpoints ─────────────────────────────────────
    #
    # These endpoints let OpenAI-style callers (e.g. OpenWebUI tool servers)
    # list and invoke the same tools exposed over MCP on port 8888 — without
    # speaking the MCP protocol.  They are a pure addition: the /mcp endpoint
    # and all /api/* endpoints are completely unaffected.
    #
    # GET  /v1/tools                    — list tools in OpenAI function-calling format
    # POST /v1/tools/{tool_name}/invoke — call a tool with {"arguments": {...}}

    @app.get("/v1/tools")
    async def list_openai_tools() -> dict:
        """Return all registered tools in OpenAI function-calling schema format.

        Response shape::

            {
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "playwright__browser_navigate",
                            "description": "...",
                            "parameters": { <JSON Schema> }
                        }
                    },
                    ...
                ]
            }
        """
        import tool_registry
        tools_out = []
        for name, entry in tool_registry.get_all().items():
            spec = entry["spec"]
            input_schema = spec.get("input_schema") or {
                "type": "object",
                "properties": {},
                "required": [],
            }
            tools_out.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": spec.get("description", ""),
                    "parameters": input_schema,
                },
            })
        return {"tools": tools_out}

    @app.post("/v1/tools/{tool_name}/invoke")
    async def invoke_openai_tool(tool_name: str, request: Request) -> dict:
        """Invoke a registered tool by name with caller-supplied arguments.

        Request body::

            {"arguments": {"param1": "value1", ...}}

        Success response::

            {
                "type": "tool_result",
                "content": [{"type": "text", "text": "..."}],
                "is_error": false
            }

        Error response (tool not found → HTTP 404; handler exception → HTTP 200 with
        ``is_error: true`` so that LLM callers can see the error message as a tool
        result rather than receiving a 5xx)::

            {
                "type": "tool_result",
                "content": [{"type": "text", "text": "<error message>"}],
                "is_error": true
            }
        """
        import tool_registry
        entry = tool_registry.get(tool_name)
        if entry is None:
            raise HTTPException(404, f"Tool '{tool_name}' not found")

        try:
            body = await request.json()
        except Exception:
            body = {}
        arguments: dict[str, Any] = (body.get("arguments") if isinstance(body, dict) else None) or {}
        handler = entry["handler"]

        try:
            # dynamic_tool signature: (ctx: Context, **kwargs).
            # Passing ctx=None is safe — build_runtime_context stores it as
            # {"mcp_context": None} and tool handlers that don't use MCP
            # context features won't notice.
            result = await handler(None, **arguments)

            # Normalise the result to a content array so callers always get a
            # consistent shape regardless of what the underlying handler returns.
            if isinstance(result, str):
                content = [{"type": "text", "text": result}]
            elif isinstance(result, dict) and "content" in result:
                content = result["content"]
            elif isinstance(result, list):
                content = result
            else:
                content = [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]

            return {"type": "tool_result", "content": content, "is_error": False}

        except Exception as exc:
            traceback.print_exc()
            return {
                "type": "tool_result",
                "content": [{"type": "text", "text": str(exc)}],
                "is_error": True,
            }

    # ── Restart ───────────────────────────────────────────────────────────────

    @app.post("/api/restart")
    async def restart() -> dict:
        def _send():
            import time; time.sleep(0.4)
            try:
                os.kill(os.getpid(), signal.SIGTERM)
            except Exception:
                pass
        threading.Thread(target=_send, daemon=True).start()
        return {"ok": True}

    # ── Client config (feature flags) ──────────────────────────────────────────

    @app.get("/api/config")
    async def client_config() -> dict:
        """Expose UI feature flags so the front end can hide disabled features."""
        return {"ok": True, "web_terminal": _web_terminal_enabled()}

    # ── Interactive web terminal (PTY over WebSocket) ──────────────────────────

    @app.websocket("/ws/terminal")
    async def terminal_ws(ws: WebSocket) -> None:
        """Bridge a browser xterm.js session to a PTY-backed subprocess.

        With an optional ``?cmd=`` query the given command is run (used by the
        wizard / Re-authorize buttons to launch ``npx -y mcp-remote <url>`` so
        the OAuth bootstrap can be completed entirely from the browser); with no
        ``cmd`` an interactive login shell is started.  The subprocess inherits
        the proxy's environment, so MCP_REMOTE_CONFIG_DIR (set by compose) points
        the token cache at the persisted volume automatically.
        """
        await ws.accept()
        if not _web_terminal_enabled():
            await ws.send_text("\r\n[web terminal disabled — set MCPPROXY_WEB_TERMINAL=1 to enable]\r\n")
            await ws.close(code=1008)
            return

        cmd = (ws.query_params.get("cmd") or "").strip()
        shell = shutil.which("bash") or shutil.which("sh") or "/bin/sh"
        argv = [shell, "-lc", cmd] if cmd else [shell, "-il"]

        pid, master_fd = pty.fork()
        if pid == 0:
            # Child: exec the shell/command with the PTY as its controlling tty.
            try:
                os.execvpe(argv[0], argv, os.environ.copy())
            except Exception:
                os._exit(127)

        loop = asyncio.get_running_loop()

        def _set_winsize(rows: int, cols: int) -> None:
            try:
                fcntl.ioctl(master_fd, termios.TIOCSWINSZ,
                            struct.pack("HHHH", max(rows, 1), max(cols, 1), 0, 0))
            except OSError:
                pass

        async def pty_to_ws() -> None:
            while True:
                try:
                    data = await loop.run_in_executor(None, os.read, master_fd, 65536)
                except OSError:
                    data = b""  # PTY closed (child exited) → EIO on Linux
                if not data:
                    break
                await ws.send_bytes(data)

        async def ws_to_pty() -> None:
            while True:
                msg = await ws.receive()
                if msg.get("type") == "websocket.disconnect":
                    break
                text = msg.get("text")
                if text is not None:
                    try:
                        ctrl = json.loads(text)
                    except (ValueError, TypeError):
                        ctrl = None
                    if isinstance(ctrl, dict) and "resize" in ctrl:
                        cols, rows = ctrl["resize"]
                        _set_winsize(int(rows), int(cols))
                        continue
                    if isinstance(ctrl, dict) and "input" in ctrl:
                        os.write(master_fd, str(ctrl["input"]).encode())
                        continue
                    os.write(master_fd, text.encode())
                elif msg.get("bytes") is not None:
                    os.write(master_fd, msg["bytes"])

        reader = asyncio.ensure_future(pty_to_ws())
        writer = asyncio.ensure_future(ws_to_pty())
        try:
            await asyncio.wait({reader, writer}, return_when=asyncio.FIRST_COMPLETED)
        except WebSocketDisconnect:
            pass
        finally:
            for task in (reader, writer):
                task.cancel()
            try:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
            except OSError:
                pass
            try:
                os.close(master_fd)
            except OSError:
                pass
            try:
                await ws.close()
            except RuntimeError:
                pass

    # ── HTML UI ───────────────────────────────────────────────────────────────

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return _HTML

    return app


# ---------------------------------------------------------------------------
# HTML — form-based single-page app (no raw YAML exposed to the user)
# ---------------------------------------------------------------------------

_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>mcpproxy — Tool Manager</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
<link href="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/codemirror.min.css" rel="stylesheet">
<link href="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/theme/dracula.min.css" rel="stylesheet">
<link href="https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.min.css" rel="stylesheet">
<style>
:root{--bg:#1e1e2e;--surface:#181825;--border:#313244;--muted:#a6adc8;--accent:#89b4fa;--green:#a6e3a1;--red:#f38ba8;--yellow:#f9e2af;--teal:#94e2d5}
*{box-sizing:border-box}
body{background:var(--bg);color:#cdd6f4;min-height:100vh;font-size:14px}
.navbar{background:var(--surface)!important;border-bottom:1px solid var(--border)}
.navbar-brand{font-weight:700;letter-spacing:.5px;color:#cdd6f4!important}
/* cards */
.card{background:var(--surface);border:1px solid var(--border);color:#cdd6f4;border-radius:8px}
.card-header{background:var(--border);border-bottom:1px solid #45475a;font-weight:600;padding:.5rem .75rem;border-radius:8px 8px 0 0;color:#cdd6f4}
.card-body{padding:.75rem}
/* provider list */
.provider-item{cursor:pointer;padding:9px 12px;border-bottom:1px solid var(--border);transition:background .15s;display:flex;justify-content:space-between;align-items:flex-start}
.provider-item:hover{background:#252535}
.provider-item.active{background:#2a2a3e;border-left:3px solid var(--accent)}
/* form controls */
.form-control,.form-select{background:#252535;border:1px solid #45475a;color:#cdd6f4;border-radius:6px}
.form-control:focus,.form-select:focus{background:#252535;border-color:var(--accent);color:#cdd6f4;box-shadow:0 0 0 2px rgba(137,180,250,.2)}
.form-control::placeholder{color:#585b70}
.form-label{color:var(--muted);font-size:.8em;margin-bottom:.2rem;font-weight:500;text-transform:uppercase;letter-spacing:.4px}
/* section boxes */
.section-box{background:#252535;border:1px solid var(--border);border-radius:8px;padding:12px;margin-bottom:12px}
.section-title{font-size:.75em;font-weight:700;text-transform:uppercase;letter-spacing:.5px;color:var(--muted);margin-bottom:10px;display:flex;justify-content:space-between;align-items:center}
/* tool cards */
.tool-card{background:var(--bg);border:1px solid var(--border);border-radius:8px;margin-bottom:10px}
.tool-card-header{padding:8px 12px;display:flex;justify-content:space-between;align-items:center;cursor:pointer;border-radius:8px 8px 0 0}
.tool-card-header:hover{background:#252535}
.tool-card-body{padding:12px;border-top:1px solid var(--border)}
/* param rows */
.param-row{display:grid;grid-template-columns:1fr 120px 2fr auto;gap:8px;align-items:start;margin-bottom:6px;background:var(--surface);border:1px solid var(--border);border-radius:6px;padding:8px}
.secret-row{display:grid;grid-template-columns:1fr 1fr auto;gap:8px;align-items:center;margin-bottom:6px;background:var(--surface);border:1px solid var(--border);border-radius:6px;padding:8px}
.list-row{display:grid;grid-template-columns:1fr auto;gap:8px;align-items:center;margin-bottom:6px}
/* CodeMirror */
.CodeMirror{height:260px;font-size:13px;border-radius:0 0 6px 6px;font-family:'JetBrains Mono',Consolas,monospace;border:1px solid #45475a;border-top:none}
.cm-wrap{border:1px solid #45475a;border-radius:6px;overflow:hidden}
.cm-label{background:#313244;padding:4px 10px;font-size:.75em;color:var(--muted);border:1px solid #45475a;border-bottom:none;border-radius:6px 6px 0 0;font-weight:600;text-transform:uppercase;letter-spacing:.4px}
/* badges */
.badge-pkg{background:#cba6f7;color:#1e1e2e;font-size:.65em;padding:2px 6px;border-radius:3px;font-weight:700}
.badge-code{background:#89b4fa;color:#1e1e2e;font-size:.65em;padding:2px 6px;border-radius:3px;font-weight:700}
.badge-repo{background:#a6e3a1;color:#1e1e2e;font-size:.65em;padding:2px 6px;border-radius:3px;font-weight:700}
.badge-count{background:#45475a;color:#cdd6f4;font-size:.65em;padding:2px 6px;border-radius:3px}
/* modal */
.modal-content{background:var(--bg);border:1px solid var(--border);color:#cdd6f4}
.modal-header,.modal-footer{border-color:var(--border)}
.btn-close-white{filter:invert(1)}
/* misc */
.restart-bar{background:#2a1f1f;border:1px solid #f38ba860;border-radius:6px;padding:8px 14px;color:#cdd6f4;display:flex;align-items:center;gap:10px;font-size:.875em}
.restart-bar .warn-icon{color:var(--yellow)}
a{color:var(--accent)}
code{color:var(--teal);background:#252535;padding:1px 4px;border-radius:3px;font-size:.85em}
.btn-icon{background:none;border:none;color:var(--muted);cursor:pointer;padding:2px 6px;font-size:.9em;line-height:1}
.btn-icon:hover{color:var(--red)}
.text-muted{color:var(--muted)!important}
.empty-state{color:var(--muted);text-align:center;padding:30px;font-size:.9em}
.wizard-choice{cursor:pointer;transition:border-color .15s,background .15s;border:1px solid var(--border)!important}
.wizard-choice:hover,.wizard-choice.selected{border-color:var(--accent)!important;background:#252535!important}
.wizard-choice .best-for{display:block;margin-top:8px;font-size:.78em;font-style:italic;color:var(--accent);opacity:.85}
.wizard-step{display:none}.wizard-step.active{display:block}
.secret-set{border-left:3px solid var(--green)!important}
.secret-unset{border-left:3px solid var(--yellow)!important}
.req-badge{font-size:.65em;padding:1px 5px;border-radius:3px;font-weight:700}
.req-yes{background:#f38ba8;color:#1e1e2e}
.req-no{background:#45475a;color:#cdd6f4}
.badge-warn{background:var(--yellow);color:#1e1e2e;font-size:.62em;padding:2px 6px;border-radius:3px;font-weight:700}
.badge-err{background:var(--red);color:#1e1e2e;font-size:.62em;padding:2px 6px;border-radius:3px;font-weight:700}
.badge-disabled{background:#45475a;color:var(--muted);font-size:.62em;padding:2px 6px;border-radius:3px;font-weight:700;text-transform:uppercase;letter-spacing:.4px}
.tool-card.disabled .tool-card-body{opacity:.55;filter:saturate(.6)}
.tool-card.disabled .tool-card-header{background:#1f1f2c}
.fn-pick-row{display:flex;gap:6px;align-items:stretch}
.fn-pick-row .form-select{max-width:220px;flex:0 0 auto}
.fn-pick-row .form-control{flex:1 1 auto;min-width:0}
.fn-status{font-size:.75em;color:var(--muted);margin-top:4px}
.fn-status.error{color:var(--red)}
.fn-status.ok{color:var(--green)}
.fn-status.busy{color:var(--accent)}
.form-check-input{background-color:#1e1e2e;border-color:#45475a}
.form-check-input:checked{background-color:var(--accent);border-color:var(--accent)}
</style>
</head>
<body>

<!-- Navbar -->
<nav class="navbar navbar-dark navbar-expand px-3">
  <span class="navbar-brand">⚡ mcpproxy</span>
  <div class="d-flex gap-2 ms-3">
    <button class="btn btn-sm btn-success" onclick="openWizard()">+ New Provider</button>
    <button class="btn btn-sm btn-outline-info" onclick="openCatalog()"
      title="Browse known MCP servers and REST/OpenAPI APIs and configure them in one click">🗂 Browse</button>
    <button class="btn btn-sm btn-outline-light" id="terminal-btn" style="display:none"
      onclick="openTerminal()" title="Open an interactive shell in the server container">🖥 Terminal</button>
    <button class="btn btn-sm btn-outline-light" onclick="openFiles()"
      title="Browse and manage the mounted tools / files / repos directories">📁 Files</button>
    <button class="btn btn-sm btn-outline-light" onclick="openToolTester()"
      title="Invoke any registered tool with custom arguments">🧪 Test Tools</button>
  </div>
  <span class="ms-auto text-muted" style="font-size:.75em">MCP :8888 &nbsp;|&nbsp; UI :8889</span>
</nav>

<!-- Pending OAuth authorization banner (surfaced by the startup warm-up) -->
<div id="auth-banner" class="restart-bar" style="display:none;background:#2a2518;border-color:#f9e2af60;border-radius:0;margin:0">
  <span style="color:var(--yellow)">🔐</span>
  <span id="auth-banner-msg" style="color:#cdd6f4;font-size:.875em"></span>
</div>

<!-- Toast -->
<div class="toast-container position-fixed top-0 end-0 p-3" style="z-index:9999">
  <div id="toast" class="toast text-white border-0" role="alert">
    <div class="d-flex">
      <div class="toast-body" id="toast-msg"></div>
      <button type="button" class="btn-close btn-close-white me-2 m-auto" data-bs-dismiss="toast"></button>
    </div>
  </div>
</div>

<!-- Main layout -->
<div class="container-fluid mt-3">
  <div class="row g-3">

    <!-- Provider list -->
    <div class="col-md-3">
      <div class="card h-100">
        <div class="card-header d-flex justify-content-between align-items-center">
          Providers
          <button class="btn btn-sm btn-outline-secondary py-0 px-2" onclick="loadList()" title="Refresh">↻</button>
        </div>
        <div id="provider-list"><div class="p-3 text-muted" style="font-size:.85em">Loading…</div></div>
      </div>
    </div>

    <!-- Editor panel -->
    <div class="col-md-9" id="editor-col">
      <div id="empty-panel" class="empty-state" style="margin-top:80px">
        Select a provider from the list, or click <b>+ New Provider</b> to create one.
      </div>
      <div id="editor-panel" style="display:none">

        <!-- Toolbar -->
        <div class="d-flex justify-content-between align-items-center mb-2">
          <h6 id="editor-title" class="mb-0 fw-semibold" style="color:#cdd6f4"></h6>
          <div class="d-flex gap-2">
            <button class="btn btn-sm btn-outline-info" onclick="openSecretsModal()">🔑 Secrets</button>
            <button class="btn btn-sm btn-primary" onclick="saveProvider()">Save</button>
            <button class="btn btn-sm btn-outline-danger" onclick="deleteProvider()">Delete</button>
          </div>
        </div>

        <!-- Restart bar -->
        <div class="restart-bar mb-2" id="restart-bar" style="display:none">
          <span class="warn-icon">⚠️</span>
          <span style="color:#cdd6f4">Changes take effect after restarting the MCP server.</span>
          <button class="btn btn-sm btn-outline-warning ms-auto" onclick="restartServer()">Restart MCP Server</button>
        </div>

        <!-- Missing secrets bar -->
        <div class="restart-bar mb-2" id="secrets-bar" style="display:none;background:#2a2518;border-color:#f9e2af60">
          <span style="color:var(--yellow)">⚠</span>
          <span id="secrets-bar-msg" style="color:#cdd6f4;font-size:.875em"></span>
          <button class="btn btn-sm btn-outline-warning ms-auto py-0 px-2" style="font-size:.8em" onclick="openSecretsModal()">🔑 Fix Secrets</button>
        </div>

        <!-- Validation error bar -->
        <div class="restart-bar mb-2" id="validation-bar" style="display:none;background:#2a1f1f;border-color:#f38ba860">
          <span style="color:var(--red)">✗</span>
          <span id="validation-bar-msg" style="color:#cdd6f4;font-size:.875em"></span>
        </div>

        <!-- Documentation box -->
        <div class="section-box">
          <div class="section-title">📖 Documentation <span class="text-muted fw-normal" style="text-transform:none;letter-spacing:0;font-size:.9em">optional — shown in the UI, not sent to LLM</span></div>
          <textarea id="f-documentation" class="form-control" rows="3" placeholder="Describe what this provider does, its tools, any usage notes…" style="font-size:.875em;resize:vertical"></textarea>
        </div>

        <!-- Package command box -->
        <div class="section-box" id="package-box" style="display:none">
          <div class="section-title">
            📦 Package Command
            <button class="btn btn-sm btn-outline-warning py-0" id="reauth-btn" style="display:none"
              onclick="reauthorizeProvider()"
              title="Re-run the mcp-remote OAuth flow in a terminal to refresh a lapsed token">🔐 Re-authorize</button>
          </div>
          <input id="f-command" class="form-control font-monospace"
            placeholder="npx @playwright/mcp@latest --isolated  ·  uvx mcp-server-fetch  ·  python -m mcp_server_github  ·  mcp-server-github"
            style="font-size:.875em"
            onblur="discoverFunctions().catch(() => {})">
          <div class="mt-2 text-muted" style="font-size:.8em">Any command that spawns a stdio MCP server: <code>npx</code>, <code>uvx</code>, <code>python -m</code>, or an installed binary. The process is started on demand and kept alive between calls.</div>
        </div>

        <!-- Repository box -->
        <div class="section-box" id="repository-box" style="display:none">
          <div class="section-title">
            📂 Repository
            <button class="btn btn-sm btn-outline-secondary py-0" onclick="rebuildRepository()" title="Clone (or pull) and re-run build commands">↻ Re-clone &amp; build</button>
          </div>
          <div class="mb-2">
            <label class="form-label">Git URL</label>
            <input id="f-repo-url" class="form-control form-control-sm font-monospace"
              placeholder="https://github.com/owner/repo"
              oninput="updateRepoField('repo_url', this.value)">
          </div>
          <div class="mb-2">
            <label class="form-label">Ref <span class="text-muted fw-normal" style="text-transform:none">optional</span></label>
            <input id="f-repo-ref" class="form-control form-control-sm font-monospace"
              placeholder="main"
              oninput="updateRepoField('repo_ref', this.value)">
          </div>
          <div class="mb-2">
            <label class="form-label d-flex justify-content-between align-items-center">
              <span>Build commands</span>
              <button class="btn btn-sm btn-outline-secondary py-0" onclick="addBuildCommand()">+ Add</button>
            </label>
            <div id="build-commands-container"></div>
            <div class="text-muted" style="font-size:.8em">Shell commands run inside the workdir before the MCP server is spawned. Re-runs on every server start. Don't put long-running server start commands here.</div>
          </div>
          <div class="mb-2">
            <label class="form-label d-flex justify-content-between align-items-center">
              <span>Env keys <span class="text-muted fw-normal" style="text-transform:none">— discovered from .env.example; values live in Secrets</span></span>
              <span class="d-flex gap-2">
                <button class="btn btn-sm btn-outline-secondary py-0" onclick="rescanEnvExample()" title="Re-scan .env.example in the workdir">↻ Re-scan</button>
                <button class="btn btn-sm btn-outline-secondary py-0" onclick="addEnvKey()">+ Add</button>
              </span>
            </label>
            <div id="env-keys-container"></div>
            <div class="text-muted" style="font-size:.8em">A <code>.env</code> file is written into the workdir from your secrets before every build / spawn — so dotenv loaders (<code>tsx --env-file=.env</code>, etc.) pick them up.</div>
          </div>
          <div class="text-muted" style="font-size:.8em">Workdir: <code id="f-repo-workdir">(auto)</code></div>
          <div id="rebuild-status" class="mt-2 fn-status"></div>
        </div>

        <!-- REST box (rest providers) -->
        <div class="section-box" id="rest-box" style="display:none">
          <div class="section-title">
            🔌 REST API
            <button class="btn btn-sm btn-outline-warning py-0" id="rest-authorize-btn" style="display:none"
              onclick="authorizeRestProvider()"
              title="Run the OAuth authorization_code flow to obtain / refresh a token">🔐 Authorize</button>
          </div>
          <div class="mb-2">
            <label class="form-label">Base URL</label>
            <input id="f-rest-base-url" class="form-control form-control-sm font-monospace"
              placeholder="https://api.example.com/v1" oninput="updateRestBaseUrl(this.value)">
          </div>
          <div class="mb-2">
            <label class="form-label">Authentication</label>
            <select id="f-rest-auth-type" class="form-select form-select-sm" onchange="updateRestAuthType(this.value)">
              <option value="none">None</option>
              <option value="bearer">Bearer token (from env)</option>
              <option value="api_key">API key header (from env)</option>
              <option value="client_credentials">OAuth2 — client credentials</option>
              <option value="authorization_code">OAuth2 — authorization code + PKCE</option>
            </select>
            <div id="f-rest-auth-fields" class="mt-2"></div>
          </div>
          <div class="mb-2">
            <label class="form-label d-flex justify-content-between align-items-center">
              <span>Default headers <span class="text-muted fw-normal" style="text-transform:none">— sent on every request</span></span>
              <button class="btn btn-sm btn-outline-secondary py-0" onclick="addRestHeader()">+ Add header</button>
            </label>
            <div id="rest-headers-container"></div>
          </div>
          <div class="mb-2">
            <label class="form-label d-flex justify-content-between align-items-center">
              <span>Endpoints <span class="text-muted fw-normal" style="text-transform:none">— each maps 1:1 to a tool by name</span></span>
              <button class="btn btn-sm btn-outline-secondary py-0" onclick="addRestEndpoint()">+ Add endpoint</button>
            </label>
            <div id="rest-endpoints-container"></div>
            <div class="text-muted mt-1" style="font-size:.8em">Path params use <code>{name}</code> in the path. List each param under the column that decides where it's sent (path / query / body). <b>⟳ Sync</b> regenerates the matching tool's input schema from these params.</div>
          </div>
          <div id="rest-auth-status" class="mt-2 fn-status"></div>
        </div>

        <!-- OAuth bootstrap box (providers with a top-level oauth: block) -->
        <div class="section-box" id="oauth-box" style="display:none">
          <div class="section-title">
            🔐 OAuth Token Bootstrap
            <button class="btn btn-sm btn-outline-warning py-0" id="oauth-bootstrap-btn"
              onclick="authorizeOAuthProvider()"
              title="Run the browser consent flow to mint / refresh the token file">🔐 Authorize</button>
          </div>
          <div id="oauth-summary" style="font-size:.875em"></div>
          <div class="text-muted mt-2" style="font-size:.8em">
            Declared by the <code>oauth:</code> block in this provider's YAML. After you approve in the
            browser, the token file is written automatically — Desktop ("installed") Google clients accept
            the localhost redirect without registration; "web" clients must have the redirect URI below
            registered in the Google Cloud Console.
          </div>
          <div id="oauth-auth-status" class="mt-2 fn-status"></div>
        </div>

        <!-- Code box (code providers) -->
        <div class="section-box" id="code-box" style="display:none">
          <div class="section-title">🐍 Python Code</div>
          <div class="cm-label">code</div>
          <textarea id="f-code"></textarea>
        </div>

        <!-- Requirements box (both types) -->
        <div class="section-box">
          <div class="section-title">
            📦 pip Requirements <span class="text-muted fw-normal" style="text-transform:none;letter-spacing:0;font-size:.9em">optional</span>
            <button class="btn btn-sm btn-outline-secondary py-0" onclick="addRequirement()">+ Add</button>
          </div>
          <div id="requirements-container"></div>
          <div class="text-muted" style="font-size:.8em">pip packages installed before the server starts (e.g. <code>httpx</code>, <code>requests==2.32.0</code>)</div>
        </div>

        <!-- Setup Commands box (both types) -->
        <div class="section-box">
          <div class="section-title">
            ⚙ Setup Commands <span class="text-muted fw-normal" style="text-transform:none;letter-spacing:0;font-size:.9em">optional</span>
            <button class="btn btn-sm btn-outline-secondary py-0" onclick="addSetupCommand()">+ Add</button>
          </div>
          <div id="setup-commands-container"></div>
          <div class="text-muted" style="font-size:.8em">Shell commands run automatically on every server startup (e.g. <code>npx playwright install chrome</code>)</div>
        </div>

        <!-- Tools box -->
        <div class="section-box">
          <div class="section-title">
            <span>🔧 Tools</span>
            <div class="d-flex gap-2 align-items-center">
              <button class="btn btn-sm btn-outline-secondary py-0" onclick="discoverFunctions().catch(() => {})" title="Re-run function discovery">↻ Re-scan</button>
              <button class="btn btn-sm btn-outline-secondary py-0" onclick="addTool()">+ Add Tool</button>
            </div>
          </div>
          <div id="fn-discovery-status" class="fn-status"></div>
          <div id="tools-container">
            <div class="empty-state" style="padding:16px">No tools yet — click <b>+ Add Tool</b>.</div>
          </div>
        </div>

      </div>
    </div>

  </div>
</div>

<!-- Secrets modal -->
<div class="modal fade" id="secrets-modal" tabindex="-1">
  <div class="modal-dialog modal-lg">
    <div class="modal-content">
      <div class="modal-header">
        <h5 class="modal-title">🔑 Secrets — <span id="secrets-provider-name"></span></h5>
        <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button>
      </div>
      <div class="modal-body">
        <p class="text-muted" style="font-size:.85em">Values are written to <code id="secrets-env-path">.env</code>. The file is never committed to git.</p>
        <div id="secrets-fields"></div>
      </div>
      <div class="modal-footer">
        <button class="btn btn-outline-secondary" data-bs-dismiss="modal">Cancel</button>
        <button class="btn btn-primary" onclick="saveSecrets()">Save to .env</button>
      </div>
    </div>
  </div>
</div>

<!-- Wizard modal -->
<div class="modal fade" id="wizard-modal" tabindex="-1">
  <div class="modal-dialog modal-lg modal-dialog-scrollable">
    <div class="modal-content">
      <div class="modal-header">
        <h5 class="modal-title" id="wizard-title">New MCP Provider</h5>
        <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button>
      </div>
      <div class="modal-body">

        <!-- Step: choose type -->
        <div id="wz-type" class="wizard-step active">
          <p class="text-muted mb-4">How do you want to create this provider?</p>
          <div class="row g-3">
            <div class="col-md-3">
              <div class="card wizard-choice h-100" onclick="wzSelectType('remote')">
                <div class="card-body text-center p-4">
                  <div style="font-size:2.5em">🌐</div>
                  <h6 class="mt-2">Remote MCP Server</h6>
                  <small class="text-muted">Bridge a remote, OAuth-protected MCP server — just paste its URL (e.g. the official Asana server). Tools &amp; auth are handled automatically.</small>
                  <small class="best-for">Best for hosted SaaS tools that already speak MCP — e.g. Asana, Linear, Notion, GitHub — where you just have a URL.</small>
                </div>
              </div>
            </div>
            <div class="col-md-3">
              <div class="card wizard-choice h-100" onclick="wzSelectType('package')">
                <div class="card-body text-center p-4">
                  <div style="font-size:2.5em">📦</div>
                  <h6 class="mt-2">Package</h6>
                  <small class="text-muted">Run an existing MCP server via <code>npx</code>, <code>uvx</code>, <code>python -m</code>, or any command — or bridge a remote server with <code>npx -y mcp-remote &lt;url&gt;</code>. Tools are auto-detected.</small>
                  <small class="best-for">Best for published MCP servers you install and run locally — e.g. Playwright, filesystem, Slack, Puppeteer via <code>npx</code>/<code>uvx</code>/<code>pip</code>.</small>
                </div>
              </div>
            </div>
            <div class="col-md-3">
              <div class="card wizard-choice h-100" onclick="wzSelectType('repository')">
                <div class="card-body text-center p-4">
                  <div style="font-size:2.5em">📂</div>
                  <h6 class="mt-2">Repository</h6>
                  <small class="text-muted">Clone a git repo, run build commands, then introspect & spawn the resulting stdio MCP server</small>
                  <small class="best-for">Best for MCP servers distributed as source you build yourself before running.</small>
                </div>
              </div>
            </div>
            <div class="col-md-3">
              <div class="card wizard-choice h-100" onclick="wzSelectType('rest')">
                <div class="card-body text-center p-4">
                  <div style="font-size:2.5em">🔌</div>
                  <h6 class="mt-2">REST / OAuth API</h6>
                  <small class="text-muted">Point at a REST API (base URL + endpoints, or an OpenAPI spec) with optional OAuth — each endpoint becomes an MCP tool</small>
                  <small class="best-for">Best for any plain web API that has no prebuilt MCP server — e.g. Stripe, OpenWeather, or an internal service — described by a base URL or OpenAPI spec.</small>
                </div>
              </div>
            </div>
            <div class="col-md-3">
              <div class="card wizard-choice h-100" onclick="wzSelectType('code')">
                <div class="card-body text-center p-4">
                  <div style="font-size:2.5em">🐍</div>
                  <h6 class="mt-2">Python Code</h6>
                  <small class="text-muted">Write <code>async def</code> functions — each one becomes an MCP tool</small>
                  <small class="best-for">Best for quick custom logic, glue, or calculations you write inline — no external server needed.</small>
                </div>
              </div>
            </div>
          </div>
        </div>

        <!-- Step: remote MCP server (URL → mcp-remote bridge) -->
        <div id="wz-remote" class="wizard-step">
          <div class="mb-3">
            <label class="form-label">Provider name</label>
            <input class="form-control" id="wz-remote-name" placeholder="asana">
          </div>
          <div class="mb-3">
            <label class="form-label">Server URL *</label>
            <input class="form-control font-monospace" id="wz-remote-url"
              placeholder="https://mcp.asana.com/v2/mcp">
            <div class="text-muted mt-1" style="font-size:.8em">The remote MCP server endpoint. mcpproxy bridges it with <code>npx -y mcp-remote &lt;url&gt;</code> — transport is auto-detected.</div>
          </div>
          <div class="text-muted" style="font-size:.8em">When you click <b>Next</b> the server is introspected automatically; its tools become the dropdown options in the editor. If the server is OAuth-protected, a clickable <b>🔐 Authorize</b> link appears — complete the browser flow and introspection continues. The token is cached and refreshed automatically afterwards.</div>
          <div class="mt-2" id="wz-remote-bootstrap-wrap" style="display:none">
            <button class="btn btn-sm btn-outline-warning py-0" onclick="wzBootstrapRemote()"
              title="Run npx -y mcp-remote <url> in a terminal and complete the OAuth flow before introspecting">🖥 Bootstrap / Authorize in terminal</button>
            <span class="text-muted ms-2" style="font-size:.8em">Headless option: watch the live <code>mcp-remote</code> output, click the auth link, and pre-populate the token cache.</span>
          </div>
          <div id="wz-remote-result" class="mt-2"></div>
        </div>

        <!-- Step: package command -->
        <div id="wz-package" class="wizard-step">
          <div class="mb-3">
            <label class="form-label">Provider name</label>
            <input class="form-control" id="wz-pkg-name" placeholder="playwright">
          </div>
          <div class="mb-3">
            <label class="form-label">Command *</label>
            <input class="form-control font-monospace" id="wz-pkg-cmd"
              placeholder="npx @playwright/mcp@latest  ·  uvx mcp-server-fetch  ·  python -m mcp_server_github">
            <div class="text-muted mt-1" style="font-size:.8em">Any command that spawns a stdio MCP server (npx, uvx, python -m, or an installed binary). To bridge a remote, OAuth-protected MCP server, use <code>npx -y mcp-remote &lt;url&gt;</code> (or pick the <b>Remote MCP Server</b> option, which builds this for you).</div>
          </div>
          <div class="mb-3">
            <label class="form-label">pip Requirements <span class="text-muted fw-normal" style="text-transform:none">optional</span></label>
            <div id="wz-pkg-reqs-container"></div>
            <button class="btn btn-sm btn-outline-secondary py-0 mt-1" onclick="wzAddReq()">+ Add requirement</button>
            <div class="text-muted mt-1" style="font-size:.8em">pip packages installed before introspection and on every server restart.</div>
          </div>
          <div class="mb-3">
            <label class="form-label">Setup Commands <span class="text-muted fw-normal" style="text-transform:none">optional</span></label>
            <div id="wz-pkg-cmds-container"></div>
            <button class="btn btn-sm btn-outline-secondary py-0 mt-1" onclick="wzAddSetupCmd()">+ Add command</button>
            <div class="text-muted mt-1" style="font-size:.8em">Shell commands run on every server startup (e.g. <code>npx playwright install chrome</code>).</div>
          </div>
          <div class="text-muted" style="font-size:.8em">When you click <b>Next</b> the command is introspected automatically; tools it advertises become the dropdown options in the editor. If introspection fails you can still proceed and add tools by hand.</div>
          <div id="wz-introspect-result" class="mt-2"></div>
        </div>

        <!-- Step: repository -->
        <div id="wz-repository" class="wizard-step">
          <div class="mb-3">
            <label class="form-label">Provider name</label>
            <input class="form-control" id="wz-repo-name" placeholder="linkedin">
          </div>
          <div class="mb-3">
            <label class="form-label">Git repository URL *</label>
            <input class="form-control font-monospace" id="wz-repo-url"
              placeholder="https://github.com/felipfr/linkedin-mcpserver">
          </div>
          <div class="mb-3">
            <label class="form-label">Ref <span class="text-muted fw-normal" style="text-transform:none">optional — branch, tag, or commit SHA</span></label>
            <input class="form-control font-monospace" id="wz-repo-ref" placeholder="main">
          </div>
          <div class="mb-3">
            <label class="form-label d-flex justify-content-between align-items-center">
              <span>Build commands <span class="text-muted fw-normal" style="text-transform:none">run inside the cloned workdir, in order — must terminate</span></span>
              <button class="btn btn-sm btn-outline-secondary py-0" onclick="wzPrefillRepoNodeTs()" title="Pre-fill the Node/TypeScript defaults: npm install + npm run build, spawn node build/index.js">⚡ Pre-fill Node/TS</button>
            </label>
            <div id="wz-repo-builds-container"></div>
            <button class="btn btn-sm btn-outline-secondary py-0 mt-1" onclick="wzAddRepoBuild()">+ Add command</button>
            <div class="text-muted mt-1" style="font-size:.8em">
              <b>Recommended for Node/TypeScript repos</b>: <code>npm install</code> then <code>npm run build</code> (then spawn with <code>node build/index.js</code>). <b>Do not</b> put a long-running server start here (e.g. <code>npm run start:dev</code>) — that goes in <b>Spawn command</b>. Build commands re-run on every server start so ephemeral containers rebuild.
            </div>
          </div>
          <div class="mb-3">
            <label class="form-label">Spawn command *</label>
            <input class="form-control font-monospace" id="wz-repo-cmd"
              placeholder="node build/index.js">
            <div class="text-muted mt-1" style="font-size:.8em">The long-running command that launches the stdio MCP server, run from inside the workdir after the build commands complete. Common values: <code>node build/index.js</code> (compiled TS), <code>npx tsx src/main.ts</code> (un-compiled TS), <code>python -m my_server</code>.</div>
          </div>
          <div class="text-muted" style="font-size:.8em">Clicking <b>Next</b> clones the repo, parses <code>.env.example</code> (so its keys appear as secrets on the next step), runs the build commands, then introspects the spawn command to populate the tool list. If the build fails because secrets aren't set yet, you can still continue — the next server restart will re-build with the secrets in place.</div>
          <div id="wz-repo-result" class="mt-2"></div>
        </div>

        <!-- Step: REST / OAuth API -->
        <div id="wz-rest" class="wizard-step">
          <div class="mb-3">
            <label class="form-label">Provider name</label>
            <input class="form-control" id="wz-rest-name" placeholder="weather">
          </div>
          <div class="mb-3">
            <label class="form-label">Base URL *</label>
            <input class="form-control font-monospace" id="wz-rest-base-url"
              placeholder="https://api.example.com/v1">
            <div class="text-muted mt-1" style="font-size:.8em">Requests are sent to <code>&lt;base URL&gt;&lt;endpoint path&gt;</code>.</div>
          </div>
          <div class="mb-3">
            <label class="form-label">Authentication</label>
            <select class="form-select" id="wz-rest-auth-type" onchange="wzRestAuthChanged()">
              <option value="none">None</option>
              <option value="bearer">Bearer token (from env)</option>
              <option value="api_key">API key header (from env)</option>
              <option value="client_credentials">OAuth2 — client credentials</option>
              <option value="authorization_code">OAuth2 — authorization code + PKCE</option>
            </select>
          </div>
          <div class="mb-3" id="wz-rest-auth-fields" style="display:none">
            <!-- bearer -->
            <div class="wz-rest-auth wz-rest-auth-bearer" style="display:none">
              <label class="form-label">Token env var *</label>
              <input class="form-control font-monospace" id="wz-rest-token-env" placeholder="EXAMPLE_TOKEN">
            </div>
            <!-- api_key -->
            <div class="wz-rest-auth wz-rest-auth-api_key" style="display:none">
              <div class="row g-2">
                <div class="col">
                  <label class="form-label">Header name</label>
                  <input class="form-control font-monospace" id="wz-rest-header" placeholder="X-Api-Key">
                </div>
                <div class="col">
                  <label class="form-label">Value env var *</label>
                  <input class="form-control font-monospace" id="wz-rest-value-env" placeholder="EXAMPLE_API_KEY">
                </div>
              </div>
            </div>
            <!-- oauth shared -->
            <div class="wz-rest-auth wz-rest-auth-oauth" style="display:none">
              <div class="mb-2 wz-rest-auth-authcode-only" style="display:none">
                <label class="form-label">Authorize URL *</label>
                <input class="form-control font-monospace" id="wz-rest-authorize-url" placeholder="https://auth.example.com/oauth/authorize">
              </div>
              <div class="mb-2">
                <label class="form-label">Token URL *</label>
                <input class="form-control font-monospace" id="wz-rest-token-url" placeholder="https://auth.example.com/oauth/token">
              </div>
              <div class="row g-2 mb-2">
                <div class="col">
                  <label class="form-label">Client ID env var *</label>
                  <input class="form-control font-monospace" id="wz-rest-client-id-env" placeholder="EXAMPLE_CLIENT_ID">
                </div>
                <div class="col">
                  <label class="form-label">Client secret env var <span class="text-muted fw-normal" style="text-transform:none" id="wz-rest-secret-optional"></span></label>
                  <input class="form-control font-monospace" id="wz-rest-client-secret-env" placeholder="EXAMPLE_CLIENT_SECRET">
                </div>
              </div>
              <div class="mb-2">
                <label class="form-label">Scopes <span class="text-muted fw-normal" style="text-transform:none">space-separated</span></label>
                <input class="form-control font-monospace" id="wz-rest-scopes" placeholder="read write">
              </div>
              <div class="text-muted wz-rest-auth-authcode-only" style="display:none;font-size:.8em">
                Register this redirect URI with your OAuth provider: <code id="wz-rest-redirect-uri">http://localhost:8889/oauth/callback</code>.
                After creating the provider, click <b>🔐 Authorize</b> in the editor to complete the browser flow.
              </div>
            </div>
          </div>
          <div class="mb-3">
            <label class="form-label">Endpoints</label>
            <ul class="nav nav-tabs" style="font-size:.85em">
              <li class="nav-item"><a class="nav-link active" id="wz-rest-tab-openapi" href="#" onclick="wzRestTab('openapi');return false">Import OpenAPI</a></li>
              <li class="nav-item"><a class="nav-link" id="wz-rest-tab-manual" href="#" onclick="wzRestTab('manual');return false">Manual</a></li>
            </ul>
            <div class="pt-2" id="wz-rest-openapi-pane">
              <label class="form-label">OpenAPI URL or file path</label>
              <div class="d-flex gap-2">
                <input class="form-control font-monospace" id="wz-rest-openapi" placeholder="https://api.example.com/openapi.json">
                <button class="btn btn-outline-secondary" onclick="wzRestIntrospect()" id="wz-rest-introspect-btn">Introspect</button>
              </div>
              <div class="text-muted mt-1" style="font-size:.8em">Parses the spec into endpoints + tools when you click <b>Introspect</b> or <b>Next</b>.</div>
            </div>
            <div class="pt-2" id="wz-rest-manual-pane" style="display:none">
              <div id="wz-rest-endpoints-container"></div>
              <button class="btn btn-sm btn-outline-secondary py-0 mt-1" onclick="wzRestAddEndpoint()">+ Add endpoint</button>
              <div class="text-muted mt-1" style="font-size:.8em">Path params use <code>{name}</code>; list each param under the right column (path / query / body).</div>
            </div>
          </div>
          <div id="wz-rest-result" class="mt-2"></div>
        </div>

        <!-- Step: code paste -->
        <div id="wz-code" class="wizard-step">
          <div class="mb-3">
            <label class="form-label">Provider name</label>
            <input class="form-control" id="wz-code-name" placeholder="my_provider">
          </div>
          <div class="mb-3">
            <label class="form-label">Python code — paste <code>async def</code> functions</label>
            <textarea id="wz-code-input" class="form-control font-monospace" rows="10"
              placeholder="async def my_tool(context, query: str) -> dict:&#10;    return {'ok': True, 'result': query}"></textarea>
          </div>
          <div class="mb-3">
            <label class="form-label">pip Requirements <span class="text-muted fw-normal" style="text-transform:none">optional</span></label>
            <div id="wz-code-reqs-container"></div>
            <button class="btn btn-sm btn-outline-secondary py-0 mt-1" onclick="wzAddCodeReq()">+ Add requirement</button>
          </div>
          <div class="mb-3">
            <label class="form-label">Setup Commands <span class="text-muted fw-normal" style="text-transform:none">optional</span></label>
            <div id="wz-code-cmds-container"></div>
            <button class="btn btn-sm btn-outline-secondary py-0 mt-1" onclick="wzAddCodeSetupCmd()">+ Add command</button>
          </div>
          <div class="text-muted" style="font-size:.8em">Functions are detected automatically as you type. Each <code>async def fn(context, …)</code> becomes a tool entry.</div>
          <div id="wz-analyze-result" class="mt-2" style="font-size:.85em"></div>
        </div>

        <!-- Step: secrets -->
        <div id="wz-secrets" class="wizard-step">
          <p class="text-muted" style="font-size:.875em">The following environment variables are declared in this provider. Enter their values to save them to <code id="wz-env-path">.env</code>.</p>
          <div id="wz-secrets-fields"></div>
          <div id="wz-secrets-none" style="display:none" class="text-muted">No secrets declared in this provider.</div>
        </div>

      </div>
      <div class="modal-footer">
        <button class="btn btn-outline-secondary" id="wz-back-btn" onclick="wzBack()" style="display:none">← Back</button>
        <button class="btn btn-outline-secondary" data-bs-dismiss="modal">Cancel</button>
        <button class="btn btn-primary" id="wz-next-btn" onclick="wzNext()">Next →</button>
        <div id="wz-error" class="text-danger w-100 text-end mt-1" style="font-size:.85em"></div>
      </div>
    </div>
  </div>
</div>

<!-- Terminal modal -->
<div class="modal fade" id="terminal-modal" tabindex="-1">
  <div class="modal-dialog modal-xl modal-dialog-scrollable">
    <div class="modal-content">
      <div class="modal-header">
        <h5 class="modal-title">🖥 <span id="terminal-title">Terminal</span></h5>
        <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button>
      </div>
      <div class="modal-body">
        <div id="terminal-host" style="height:60vh;background:#000;border:1px solid var(--border);border-radius:6px;padding:6px"></div>
        <div class="text-muted mt-2" style="font-size:.8em">
          A live shell inside the server container. For an OAuth bootstrap, watch for the
          <code>authorization required … visit:</code> line, open it, and complete the browser
          flow — the token cache is written under <code>MCP_REMOTE_CONFIG_DIR</code>. Press
          <code>Ctrl-C</code> to stop a running command.
        </div>
      </div>
    </div>
  </div>
</div>

<!-- Files modal -->
<div class="modal fade" id="files-modal" tabindex="-1">
  <div class="modal-dialog modal-xl modal-dialog-scrollable">
    <div class="modal-content">
      <div class="modal-header">
        <h5 class="modal-title">📁 Files</h5>
        <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button>
      </div>
      <div class="modal-body">
        <div class="d-flex gap-2 align-items-center mb-2 flex-wrap">
          <select id="files-root" class="form-select form-select-sm" style="width:auto"
            onchange="filesSetRoot(this.value)"></select>
          <nav style="--bs-breadcrumb-divider:'/'">
            <ol class="breadcrumb mb-0" id="files-crumbs" style="font-size:.875em"></ol>
          </nav>
          <div class="ms-auto d-flex gap-2">
            <button class="btn btn-sm btn-outline-light" onclick="filesMkdir()">📁 New folder</button>
            <button class="btn btn-sm btn-outline-light"
              onclick="document.getElementById('files-upload-input').click()">⬆ Upload</button>
            <input type="file" id="files-upload-input" multiple style="display:none"
              onchange="filesUpload(this.files)">
            <button class="btn btn-sm btn-outline-light" onclick="filesRefresh()">↻</button>
          </div>
        </div>
        <div id="files-list" style="border:1px solid var(--border);border-radius:8px;min-height:200px"></div>
        <div class="text-muted mt-2" style="font-size:.8em">
          These directories are volume mounts inside the container (e.g. <code>/app/tools</code>) —
          a good place for provider configs and credential files like
          <code>tools/secrets/client_secret.json</code>.
        </div>
      </div>
    </div>
  </div>
</div>

<!-- Tool tester modal -->
<div class="modal fade" id="tooltest-modal" tabindex="-1">
  <div class="modal-dialog modal-xl modal-dialog-scrollable">
    <div class="modal-content">
      <div class="modal-header">
        <h5 class="modal-title">🧪 Test Tools</h5>
        <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button>
      </div>
      <div class="modal-body">
        <div class="row g-3">
          <div class="col-md-4">
            <input id="tt-search" class="form-control form-control-sm mb-2"
              placeholder="Filter tools…" oninput="ttRenderList()">
            <div id="tt-list" style="border:1px solid var(--border);border-radius:8px;max-height:60vh;overflow-y:auto"></div>
          </div>
          <div class="col-md-8">
            <div id="tt-detail">
              <div class="empty-state" style="margin-top:60px">Select a tool to test it.</div>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</div>

<!-- Browse providers catalog modal -->
<div class="modal fade" id="catalog-modal" tabindex="-1">
  <div class="modal-dialog modal-lg modal-dialog-scrollable">
    <div class="modal-content">
      <div class="modal-header">
        <h5 class="modal-title">🗂 Browse providers</h5>
        <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button>
      </div>
      <div class="modal-body">
        <p class="text-muted">Pick a known MCP server or REST/OpenAPI API — it opens the wizard with the details pre-filled.</p>
        <div class="d-flex flex-wrap gap-2 align-items-center mb-3">
          <input id="catalog-search" class="form-control form-control-sm" style="flex:1 1 220px"
            placeholder="Search by name or description…" oninput="catalogSearch()">
          <select id="catalog-kind" class="form-select form-select-sm" style="width:auto" onchange="renderCatalog()">
            <option value="">All types</option>
            <option value="mcp_remote">MCP servers</option>
            <option value="rest_openapi">REST / OpenAPI</option>
          </select>
          <div class="form-check form-check-inline mb-0">
            <input class="form-check-input" type="checkbox" id="catalog-live" onchange="catalogToggleLive(this)">
            <label class="form-check-label text-muted" for="catalog-live"
              title="Also query the official MCP registry, Smithery, and APIs.guru">Probe live registries</label>
          </div>
        </div>
        <div id="catalog-list"></div>
      </div>
    </div>
  </div>
</div>

<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/codemirror.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/mode/python/python.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/js/bootstrap.bundle.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.min.js"></script>
<script>
// ─────────────────────────────────────────────────────────────────────────────
// State
// ─────────────────────────────────────────────────────────────────────────────
let currentName = null;
let currentProvider = null;   // the structured JSON object being edited
let codeEditor = null;        // CodeMirror instance for the code block
let secretsModal = null, wizModal = null, termModal = null;
let catalogModal = null, catalogEntries = [];
let filesModal = null, ttModal = null;
let filesRoot = 'tools', filesPath = '', filesRoots = ['tools', 'files', 'repos'];
let ttTools = [], ttSelected = null;  // tool tester: /v1/tools entries + selected name
let webTerminalEnabled = false;
let term = null, termFit = null, termSock = null;  // xterm.js terminal state
let wzType = null;            // 'code' | 'package' | 'repository' | 'remote' | 'rest'
let wzStep = 'type';
let wzIntrospectedTools = []; // tools returned by introspect
let wzRestEndpoints = [];     // REST wizard: concrete endpoint specs
let wzRestEndpointTools = {}; // REST wizard: endpoint name → tool spec (from OpenAPI)
let wzRepoCtx = null;         // repository-wizard state carried across steps
                              //   {name, command, repo_url, repo_ref,
                              //    build_commands, workdir, env_keys, tools,
                              //    buildOk, buildErr, introErr}
let providersMeta = {};       // name → {missing_secrets, validation_errors}
let knownFunctions = [];      // names available in the current provider:
                              //   code provider    → async def names in the code block
                              //   package provider → tool names from upstream introspection
let knownFnStatus = 'idle';   // 'idle' | 'busy' | 'ok' | 'error'
let knownFnMessage = '';      // human-readable status text
let _analyzeDebounce = null;
let _wzAnalyzeDebounce = null;

// ─────────────────────────────────────────────────────────────────────────────
// Boot
// ─────────────────────────────────────────────────────────────────────────────
window.addEventListener('DOMContentLoaded', () => {
  codeEditor = CodeMirror.fromTextArea(document.getElementById('f-code'), {
    mode: 'python', theme: 'dracula', lineNumbers: true,
    indentWithTabs: false, indentUnit: 4, tabSize: 4,
  });
  codeEditor.on('change', () => {
    if (!currentProvider || currentProvider.type !== 'code') return;
    clearTimeout(_analyzeDebounce);
    _analyzeDebounce = setTimeout(() => discoverFunctions().catch(() => {}), 300);
  });
  secretsModal = new bootstrap.Modal('#secrets-modal');
  wizModal     = new bootstrap.Modal('#wizard-modal');
  termModal    = new bootstrap.Modal('#terminal-modal');
  filesModal   = new bootstrap.Modal('#files-modal');
  ttModal      = new bootstrap.Modal('#tooltest-modal');
  document.getElementById('terminal-modal').addEventListener('hidden.bs.modal', closeTerminal);
  document.getElementById('files-list').addEventListener('click', filesListClick);
  document.getElementById('tt-list').addEventListener('click', ttListClick);
  // Wizard: live function detection as the user types into the code textarea
  document.getElementById('wz-code-input').addEventListener('input', () => {
    clearTimeout(_wzAnalyzeDebounce);
    _wzAnalyzeDebounce = setTimeout(() => wzAnalyze().catch(() => {}), 300);
  });
  loadConfig();
  pollPendingAuth();
  setInterval(pollPendingAuth, 5000);
  loadList();
});

// ─────────────────────────────────────────────────────────────────────────────
// Client config + pending-auth banner
// ─────────────────────────────────────────────────────────────────────────────
async function loadConfig() {
  try {
    const c = await api('GET', '/api/config');
    webTerminalEnabled = !!c.web_terminal;
  } catch { webTerminalEnabled = false; }
  document.getElementById('terminal-btn').style.display = webTerminalEnabled ? '' : 'none';
}

async function pollPendingAuth() {
  let pending = {};
  try {
    const r = await api('GET', '/api/pending-auth');
    pending = (r && r.pending) || {};
  } catch { return; }
  const cmds = Object.keys(pending);
  const banner = document.getElementById('auth-banner');
  if (!cmds.length) { banner.style.display = 'none'; return; }
  const links = cmds.map(cmd =>
    `<a href="${esc(pending[cmd])}" target="_blank" rel="noopener">authorize ${esc(cmd.replace(/^.*mcp-remote\s+/, '') || cmd)}</a>`
  ).join(' · ');
  document.getElementById('auth-banner-msg').innerHTML =
    `Authorization required for ${cmds.length} remote provider(s): ${links} — complete the browser flow; the token refreshes automatically afterwards.`;
  banner.style.display = '';
}

// ─────────────────────────────────────────────────────────────────────────────
// API
// ─────────────────────────────────────────────────────────────────────────────
async function api(method, path, body) {
  const r = await fetch(path, {
    method,
    headers: {'Content-Type': 'application/json'},
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const data = await r.json().catch(() => ({detail: r.statusText}));
  if (!r.ok) throw new Error(data.detail || r.statusText);
  return data;
}

// ─────────────────────────────────────────────────────────────────────────────
// Toast
// ─────────────────────────────────────────────────────────────────────────────
function toast(msg, ok = true) {
  const el = document.getElementById('toast');
  el.className = `toast text-white border-0 bg-${ok ? 'success' : 'danger'}`;
  document.getElementById('toast-msg').textContent = msg;
  bootstrap.Toast.getOrCreateInstance(el, {delay: 3200}).show();
}

// ─────────────────────────────────────────────────────────────────────────────
// Provider list
// ─────────────────────────────────────────────────────────────────────────────
async function loadList() {
  try {
    const providers = await api('GET', '/api/tools');
    const el = document.getElementById('provider-list');
    if (!providers.length) {
      el.innerHTML = '<div class="p-3 text-muted" style="font-size:.85em">No providers yet — click <b>+ New Provider</b>.</div>';
      return;
    }
    providersMeta = {};
    providers.forEach(p => {
      providersMeta[p.name] = {
        missing_secrets: p.missing_secrets || [],
        validation_errors: p.validation_errors || [],
        oauth: p.oauth || null,
      };
    });
    el.innerHTML = providers.map(p => {
      const missing = p.missing_secrets || [];
      const errs = p.validation_errors || [];
      const warnBadge = missing.length
        ? `<span class="badge-warn" title="Missing: ${esc(missing.join(', '))}">⚠ ${missing.length} secret${missing.length > 1 ? 's' : ''} missing</span>`
        : '';
      const errBadge = errs.length
        ? `<span class="badge-err" title="${esc(errs.join(' · '))}">✗ ${errs.length} config error${errs.length > 1 ? 's' : ''}</span>`
        : '';
      const oauthBadge = (p.oauth && !p.oauth.has_refresh_token)
        ? `<span class="badge-warn" title="OAuth token missing — open the provider and click Authorize">🔐 authorize</span>`
        : '';
      const alertRow = (warnBadge || errBadge || oauthBadge)
        ? `<div class="d-flex gap-1 flex-wrap mt-1">${warnBadge}${errBadge}${oauthBadge}</div>`
        : '';
      const isRepo = p.is_repository || p.provider_type === 'repository';
      const isPkg = p.is_package && !isRepo;
      let badgeClass = 'badge-code';
      let badgeText = 'code';
      if (isRepo) { badgeClass = 'badge-repo'; badgeText = 'repo'; }
      else if (isPkg) { badgeClass = 'badge-pkg'; badgeText = 'pkg'; }
      return `
      <div class="provider-item ${p.name === currentName ? 'active' : ''}" onclick="openProvider('${p.name}')">
        <div style="min-width:0">
          <div class="fw-semibold">${p.name}</div>
          <small class="text-muted d-block" style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis">
            ${(p.tool_names || []).join(', ') || 'no tools'}
          </small>
          ${alertRow}
        </div>
        <div class="d-flex flex-column gap-1 align-items-end ms-1 flex-shrink-0">
          <span class="${badgeClass}">${badgeText}</span>
          <span class="badge-count">${p.tool_count}</span>
        </div>
      </div>`;
    }).join('');
  } catch(e) {
    document.getElementById('provider-list').innerHTML = `<div class="p-3 text-danger" style="font-size:.85em">${e.message}</div>`;
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Open / render provider form
// ─────────────────────────────────────────────────────────────────────────────
async function openProvider(name) {
  try {
    const p = await api('GET', `/api/tools/${name}`);
    currentName = name;
    currentProvider = p;
    knownFunctions = [];
    knownFnStatus = 'idle';
    knownFnMessage = '';
    renderProvider(p);
    document.getElementById('empty-panel').style.display = 'none';
    document.getElementById('editor-panel').style.display = 'block';
    document.getElementById('restart-bar').style.display = 'none';
    _refreshEditorBars(name);
    // highlight active in list
    document.querySelectorAll('.provider-item').forEach(el => {
      el.classList.toggle('active', el.querySelector('.fw-semibold')?.textContent === name);
    });
    // Kick off auto-discovery in the background — silent on failure.
    discoverFunctions().catch(() => {});
  } catch(e) { toast(e.message, false); }
}

// Auto-discover the set of legal function/tool names for the current provider.
// Code providers: parse the code for async def names.
// Package providers: spawn the command and ask it for tools/list.
// Failure (or absence of input) is silent — the editor falls back to free-form text.
async function discoverFunctions() {
  if (!currentProvider) return;
  // REST providers derive their tool names from the configured endpoints; no
  // subprocess introspection or code analysis is involved.
  if (currentProvider.type === 'rest') {
    knownFunctions = ((currentProvider.rest || {}).endpoints || []).map(e => e.name).filter(Boolean);
    knownFnStatus = 'ok';
    knownFnMessage = `Found ${knownFunctions.length} endpoint${knownFunctions.length === 1 ? '' : 's'}`;
    _renderKnownFnStatus();
    _refreshToolDropdowns();
    return;
  }
  const isRepo = currentProvider.type === 'repository';
  const isPkg = currentProvider.type === 'package' || isRepo;
  knownFnStatus = 'busy';
  knownFnMessage = isPkg ? 'Introspecting package…' : 'Analyzing code…';
  _renderKnownFnStatus();
  try {
    if (isPkg) {
      const cmd = (document.getElementById('f-command').value || '').trim();
      if (!cmd) {
        knownFunctions = []; knownFnStatus = 'idle'; knownFnMessage = '';
        _renderKnownFnStatus(); _refreshToolDropdowns(); return;
      }
      const r = await api('POST', '/api/introspect', {
        command: cmd,
        requirements: currentProvider.requirements || [],
        setup_commands: currentProvider.setup_commands || [],
        cwd: isRepo ? (currentProvider.workdir || '') : '',
        env_keys: isRepo ? (currentProvider.repo_env_keys || []) : [],
      });
      if (!r.ok) throw new Error(r.error || 'introspection failed');
      knownFunctions = (r.tools || []).map(t => t.name).filter(Boolean);
      knownFnStatus = 'ok';
      knownFnMessage = `Found ${knownFunctions.length} tool${knownFunctions.length === 1 ? '' : 's'}`;
    } else {
      const code = codeEditor.getValue();
      const r = await api('POST', '/api/extract-functions', {code});
      if (!r.ok) throw new Error(r.error || 'parse error');
      knownFunctions = (r.functions || []).map(f => f.name).filter(Boolean);
      knownFnStatus = 'ok';
      knownFnMessage = `Found ${knownFunctions.length} function${knownFunctions.length === 1 ? '' : 's'}`;
    }
  } catch(e) {
    knownFunctions = [];
    knownFnStatus = 'error';
    knownFnMessage = e.message || 'discovery failed';
  }
  _renderKnownFnStatus();
  _refreshToolDropdowns();
}

function _renderKnownFnStatus() {
  const el = document.getElementById('fn-discovery-status');
  if (!el) return;
  el.className = `fn-status ${knownFnStatus}`;
  el.textContent = knownFnMessage || '';
}

// Re-render every tool dropdown without rebuilding the entire form (which would
// reset focus / scroll position).  Each dropdown's inner option list is replaced.
function _refreshToolDropdowns() {
  if (!currentProvider) return;
  const isPkg = currentProvider.type === 'package' || currentProvider.type === 'repository' || currentProvider.type === 'rest';
  const field = isPkg ? 'name' : 'function';
  (currentProvider.tools || []).forEach((t, i) => {
    const sel = document.getElementById(`fn-pick-${i}`);
    if (!sel) return;
    sel.innerHTML = _fnPickOptionsHtml(t[field] || '');
  });
}

function _fnPickOptionsHtml(currentValue) {
  const opts = [`<option value="">— pick from menu —</option>`];
  for (const fn of knownFunctions) {
    opts.push(`<option value="${esc(fn)}" ${fn===currentValue?'selected':''}>${esc(fn)}</option>`);
  }
  opts.push(`<option value="__other__">Other…</option>`);
  return opts.join('');
}

function _refreshEditorBars(name) {
  const meta = providersMeta[name] || {};
  const missing = meta.missing_secrets || [];
  const errs = meta.validation_errors || [];

  const sBar = document.getElementById('secrets-bar');
  const sMsg = document.getElementById('secrets-bar-msg');
  if (missing.length) {
    sMsg.textContent = `${missing.length} secret${missing.length > 1 ? 's' : ''} not set in .env: ${missing.join(', ')}`;
    sBar.style.display = '';
  } else {
    sBar.style.display = 'none';
  }

  const vBar = document.getElementById('validation-bar');
  const vMsg = document.getElementById('validation-bar-msg');
  if (errs.length) {
    vMsg.textContent = errs.join(' · ');
    vBar.style.display = '';
  } else {
    vBar.style.display = 'none';
  }
}

function renderProvider(p) {
  const isRepo = p.type === 'repository';
  const isRest = p.type === 'rest';
  const isPkg = p.type === 'package' || isRepo; // repo also uses package.command
  const isCode = p.type === 'code';
  // REST tools (like package tools) are selected by endpoint name, not function.
  const nameDriven = isPkg || isRest;
  const label = isRepo ? ' (repository)' : isRest ? ' (rest)' : isPkg ? ' (package)' : ' (code)';
  document.getElementById('editor-title').textContent = p.name + label;
  document.getElementById('f-documentation').value = p.documentation || '';

  document.getElementById('package-box').style.display = isPkg ? '' : 'none';
  document.getElementById('repository-box').style.display = isRepo ? '' : 'none';
  document.getElementById('rest-box').style.display = isRest ? '' : 'none';
  document.getElementById('code-box').style.display = isCode ? '' : 'none';

  if (isPkg) {
    document.getElementById('f-command').value = p.command || '';
    const isRemote = /\bmcp-remote\b/.test(p.command || '');
    document.getElementById('reauth-btn').style.display =
      (isRemote && webTerminalEnabled) ? '' : 'none';
  }
  if (isRepo) {
    document.getElementById('f-repo-url').value = p.repo_url || '';
    document.getElementById('f-repo-ref').value = p.repo_ref || '';
    document.getElementById('f-repo-workdir').textContent = p.workdir || '(auto)';
    renderBuildCommands(p.build_commands || []);
    renderEnvKeys(p.repo_env_keys || []);
  }
  if (isRest) {
    renderRestEditor(p);
  }
  if (isCode) {
    codeEditor.setValue(p.code || '');
    setTimeout(() => codeEditor.refresh(), 50);
  }

  renderOauthSummary(p);

  renderRequirements(p.requirements || []);
  renderSetupCommands(p.setup_commands || []);
  renderTools(p.tools || [], nameDriven);
}

// ── OAuth bootstrap (top-level oauth: block) ─────────────────────────────────

function renderOauthSummary(p) {
  const cfg = p.oauth || {};
  const box = document.getElementById('oauth-box');
  if (!cfg.type) { box.style.display = 'none'; return; }
  box.style.display = '';
  document.getElementById('oauth-auth-status').textContent = '';
  const meta = (providersMeta[p.name] || {}).oauth || {};
  const tokenLine = meta.has_refresh_token
    ? `<span style="color:var(--green)">✓ token present (refresh ok)${meta.expiry ? ' — expires ' + esc(meta.expiry) : ''}</span>`
    : `<span style="color:var(--yellow)">✗ no usable token — click Authorize</span>`;
  const rows = [
    ['Type', esc(cfg.type)],
    ['Client secret', `<code>${esc(cfg.client_secret_file || '')}</code>`],
    ['Token file', `<code>${esc(cfg.token_file || '')}</code>`],
    ['Scopes', (cfg.scopes || []).map(s => `<code>${esc(s)}</code>`).join('<br>') || '(none)'],
    ['Status', tokenLine],
    ['Redirect URI', `<code>${esc(window.location.origin)}/oauth/callback</code>`],
  ];
  document.getElementById('oauth-summary').innerHTML = rows.map(([k, v]) =>
    `<div class="d-flex gap-2 mb-1"><span class="text-muted" style="flex:0 0 110px">${k}</span><span style="min-width:0;word-break:break-all">${v}</span></div>`
  ).join('');
}

async function authorizeOAuthProvider() {
  if (!currentName) return;
  const status = document.getElementById('oauth-auth-status');
  status.className = 'fn-status busy';
  status.textContent = 'Starting authorization…';
  try {
    const r = await api('POST', '/api/oauth-bootstrap', {name: currentName});
    if (!r.ok) throw new Error(r.error || 'authorization failed');
    window.open(r.auth_url, '_blank', 'noopener');
    status.className = 'fn-status ok';
    status.innerHTML = `Opened the consent page. After approving, the token file is written automatically. ` +
      `<a href="${esc(r.auth_url)}" target="_blank" rel="noopener">Re-open</a>`;
    // Refresh the list (and this summary) once the callback has likely landed.
    setTimeout(async () => { await loadList(); if (currentProvider) renderOauthSummary(currentProvider); }, 4000);
  } catch(e) {
    status.className = 'fn-status error';
    status.textContent = e.message || 'authorization failed';
  }
}

function updateRestBaseUrl(val) {
  ensureProvider();
  if (!currentProvider.rest) currentProvider.rest = {};
  currentProvider.rest.base_url = val;
}

// ── REST editor: auth + endpoints (inline editing) ───────────────────────────

function renderRestEditor(p) {
  const rest = p.rest || (p.rest = {});
  rest.auth = rest.auth || {type: 'none'};
  rest.endpoints = rest.endpoints || [];
  rest.headers = rest.headers || {};
  // Edit headers as an ordered array of {key,value}; serialized back to an object
  // in collectProvider so the YAML stays a plain mapping.
  rest.headerRows = Object.entries(rest.headers).map(([key, value]) => ({key, value}));
  document.getElementById('f-rest-base-url').value = rest.base_url || '';
  document.getElementById('f-rest-auth-type').value = rest.auth.type || 'none';
  document.getElementById('rest-authorize-btn').style.display =
    (rest.auth.type === 'authorization_code') ? '' : 'none';
  document.getElementById('rest-auth-status').textContent = '';
  renderRestAuthFields(rest.auth);
  renderRestHeaders(rest.headerRows);
  renderRestEndpoints(rest.endpoints);
}

function renderRestHeaders(rows) {
  const c = document.getElementById('rest-headers-container');
  if (!rows.length) { c.innerHTML = '<div class="text-muted" style="font-size:.8em">(none)</div>'; return; }
  c.innerHTML = rows.map((h, i) => `
    <div class="list-row mt-1">
      <input class="form-control form-control-sm font-monospace" placeholder="Accept" style="max-width:40%"
        value="${esc(h.key || '')}" oninput="updateRestHeader(${i},'key',this.value)">
      <input class="form-control form-control-sm font-monospace" placeholder="application/json"
        value="${esc(h.value || '')}" oninput="updateRestHeader(${i},'value',this.value)">
      <button class="btn-icon" onclick="removeRestHeader(${i})" title="Remove">✕</button>
    </div>`).join('');
}

function addRestHeader() {
  ensureProvider();
  const rest = currentProvider.rest || (currentProvider.rest = {});
  rest.headerRows = rest.headerRows || [];
  rest.headerRows.push({key: '', value: ''});
  renderRestHeaders(rest.headerRows);
}

function removeRestHeader(i) {
  ensureProvider();
  currentProvider.rest.headerRows.splice(i, 1);
  renderRestHeaders(currentProvider.rest.headerRows);
}

function updateRestHeader(i, which, val) {
  ensureProvider();
  currentProvider.rest.headerRows[i][which] = val;
}

function updateRestAuthType(val) {
  ensureProvider();
  const rest = currentProvider.rest || (currentProvider.rest = {});
  rest.auth = rest.auth || {};
  rest.auth.type = val;
  document.getElementById('rest-authorize-btn').style.display =
    (val === 'authorization_code') ? '' : 'none';
  renderRestAuthFields(rest.auth);
}

function updateRestAuthField(key, val) {
  ensureProvider();
  const auth = currentProvider.rest.auth;
  if (key === 'scopes') auth.scopes = val.split(/\s+/).filter(Boolean);
  else auth[key] = val.trim();
}

function _restAuthRow(label, key, value, placeholder) {
  return `<div class="mb-2">
    <label class="form-label">${label}</label>
    <input class="form-control form-control-sm font-monospace" value="${esc(value || '')}"
      placeholder="${placeholder}" oninput="updateRestAuthField('${key}', this.value)">
  </div>`;
}

function renderRestAuthFields(auth) {
  const c = document.getElementById('f-rest-auth-fields');
  const t = auth.type || 'none';
  let html = '';
  if (t === 'bearer') {
    html = _restAuthRow('Token env var', 'token_env', auth.token_env, 'EXAMPLE_TOKEN');
  } else if (t === 'api_key') {
    const loc = auth.in === 'query' ? 'query' : 'header';
    html = `<div class="mb-2"><label class="form-label">Send key in</label>
        <select class="form-select form-select-sm" onchange="updateRestAuthField('in', this.value); renderRestAuthFields(currentProvider.rest.auth)">
          <option value="header" ${loc === 'header' ? 'selected' : ''}>Header</option>
          <option value="query" ${loc === 'query' ? 'selected' : ''}>Query parameter</option>
        </select></div>`;
    if (loc === 'query')
      html += _restAuthRow('Query param name', 'name', auth.name, 'api_key');
    else
      html += _restAuthRow('Header name', 'header', auth.header, 'X-Api-Key');
    html += _restAuthRow('Value env var', 'value_env', auth.value_env, 'EXAMPLE_API_KEY');
  } else if (t === 'client_credentials' || t === 'authorization_code') {
    if (t === 'authorization_code')
      html += _restAuthRow('Authorize URL', 'authorize_url', auth.authorize_url, 'https://auth.example.com/oauth/authorize');
    html += _restAuthRow('Token URL', 'token_url', auth.token_url, 'https://auth.example.com/oauth/token');
    html += _restAuthRow('Client ID env var', 'client_id_env', auth.client_id_env, 'EXAMPLE_CLIENT_ID');
    const secretLabel = 'Client secret env var' + (t === 'authorization_code' ? ' (optional — PKCE)' : '');
    html += _restAuthRow(secretLabel, 'client_secret_env', auth.client_secret_env, 'EXAMPLE_CLIENT_SECRET');
    html += _restAuthRow('Scopes (space-separated)', 'scopes', (auth.scopes || []).join(' '), 'read write');
    if (t === 'authorization_code')
      html += `<div class="text-muted" style="font-size:.8em">Redirect URI to register with your OAuth provider: <code>${esc((window.location.origin || 'http://localhost:8889') + '/oauth/callback')}</code></div>`;
  }
  c.innerHTML = html;
}

function renderRestEndpoints(endpoints) {
  const c = document.getElementById('rest-endpoints-container');
  if (!endpoints.length) {
    c.innerHTML = '<div class="text-muted" style="font-size:.8em">No endpoints yet — click <b>+ Add endpoint</b>.</div>';
    return;
  }
  const methods = ['GET', 'POST', 'PUT', 'PATCH', 'DELETE'];
  c.innerHTML = endpoints.map((ep, i) => `
    <div class="border rounded p-2 mt-1">
      <div class="row g-2">
        <div class="col-4"><input class="form-control form-control-sm font-monospace" placeholder="get_user"
            value="${esc(ep.name || '')}" oninput="updateRestEndpoint(${i},'name',this.value)"></div>
        <div class="col-3">
          <select class="form-select form-select-sm" onchange="updateRestEndpoint(${i},'method',this.value)">
            ${methods.map(m => `<option ${(ep.method || 'GET') === m ? 'selected' : ''}>${m}</option>`).join('')}
          </select>
        </div>
        <div class="col-4"><input class="form-control form-control-sm font-monospace" placeholder="/users/{user_id}"
            value="${esc(ep.path || '')}" oninput="updateRestEndpoint(${i},'path',this.value)"></div>
        <div class="col-1"><button class="btn-icon" onclick="removeRestEndpoint(${i})" title="Remove">✕</button></div>
      </div>
      <div class="row g-2 mt-1" style="font-size:.8em">
        <div class="col"><input class="form-control form-control-sm font-monospace" placeholder="path params: user_id"
            value="${esc((ep.path_params || []).join(', '))}" oninput="updateRestEndpointParams(${i},'path_params',this.value)"></div>
        <div class="col"><input class="form-control form-control-sm font-monospace" placeholder="query params: include"
            value="${esc((ep.query_params || []).join(', '))}" oninput="updateRestEndpointParams(${i},'query_params',this.value)"></div>
        <div class="col"><input class="form-control form-control-sm font-monospace" placeholder="body params: title, body"
            value="${esc((ep.body_params || []).join(', '))}" oninput="updateRestEndpointParams(${i},'body_params',this.value)"></div>
      </div>
      <div class="mt-1"><button class="btn btn-sm btn-outline-secondary py-0" onclick="syncRestEndpointToTool(${i})"
          title="Create/refresh the matching tool's input schema from this endpoint's params">⟳ Sync params to tool schema</button></div>
    </div>`).join('');
}

function _uniqueEndpointName() {
  const used = new Set((currentProvider.rest.endpoints || []).map(e => e.name));
  let n = (currentProvider.rest.endpoints || []).length + 1;
  let name = `endpoint_${n}`;
  while (used.has(name)) { n++; name = `endpoint_${n}`; }
  return name;
}

function addRestEndpoint() {
  ensureProvider();
  const rest = currentProvider.rest || (currentProvider.rest = {});
  rest.endpoints = rest.endpoints || [];
  const name = _uniqueEndpointName();
  rest.endpoints.push({name, method: 'GET', path: '/', path_params: [], query_params: [], body_params: []});
  // Pair a tool with the same name so the 1:1 invariant holds and it shows in Tools.
  currentProvider.tools = currentProvider.tools || [];
  if (!currentProvider.tools.some(t => t.name === name)) {
    currentProvider.tools.push({name, function: '', description: name, documentation: '', enabled: true, parameters: [], secrets: []});
  }
  renderRestEndpoints(rest.endpoints);
  renderTools(currentProvider.tools, true);
  discoverFunctions().catch(() => {});
}

function removeRestEndpoint(i) {
  ensureProvider();
  const ep = currentProvider.rest.endpoints[i];
  currentProvider.rest.endpoints.splice(i, 1);
  if (ep && ep.name) {
    currentProvider.tools = (currentProvider.tools || []).filter(t => t.name !== ep.name);
  }
  renderRestEndpoints(currentProvider.rest.endpoints);
  renderTools(currentProvider.tools, true);
  discoverFunctions().catch(() => {});
}

function updateRestEndpoint(i, field, val) {
  ensureProvider();
  const ep = currentProvider.rest.endpoints[i];
  if (field === 'name') {
    const oldName = ep.name;
    const newName = val.trim();
    ep.name = newName;
    // Keep the paired tool's name in sync so the endpoint↔tool link is preserved.
    (currentProvider.tools || []).forEach(t => { if (t.name === oldName) t.name = newName; });
    renderTools(currentProvider.tools, true);
    discoverFunctions().catch(() => {});
  } else {
    ep[field] = val.trim();
  }
}

function updateRestEndpointParams(i, field, val) {
  ensureProvider();
  currentProvider.rest.endpoints[i][field] = val.split(',').map(s => s.trim()).filter(Boolean);
}

// Regenerate the matching tool's parameters from an endpoint's param routing.
// Preserves any existing param's type/description; path params default to required.
function syncRestEndpointToTool(i) {
  ensureProvider();
  const ep = currentProvider.rest.endpoints[i];
  const tool = (currentProvider.tools || []).find(t => t.name === ep.name);
  if (!tool) { toast('No matching tool for this endpoint', false); return; }
  const names = [...(ep.path_params || []), ...(ep.query_params || []), ...(ep.body_params || [])];
  const pathSet = new Set(ep.path_params || []);
  const existing = {};
  (tool.parameters || []).forEach(p => { existing[p.name] = p; });
  tool.parameters = names.map(n => existing[n] || {name: n, type: 'string', description: '', required: pathSet.has(n), default: null});
  renderTools(currentProvider.tools, true);
  toast(`Synced ${names.length} param(s) to ${ep.name}`);
}

async function authorizeRestProvider() {
  if (!currentName) return;
  const status = document.getElementById('rest-auth-status');
  status.className = 'fn-status busy';
  status.textContent = 'Starting authorization…';
  try {
    const r = await api('POST', '/api/rest-authorize', {name: currentName});
    if (!r.ok) throw new Error(r.error || 'authorization failed');
    window.open(r.auth_url, '_blank', 'noopener');
    status.className = 'fn-status ok';
    status.innerHTML = `Opened the authorization page. After approving, tokens are cached automatically. ` +
      `<a href="${esc(r.auth_url)}" target="_blank" rel="noopener">Re-open</a>`;
  } catch(e) {
    status.className = 'fn-status error';
    status.textContent = e.message || 'authorization failed';
  }
}

// Build commands list (repository providers)
function renderBuildCommands(cmds) {
  const c = document.getElementById('build-commands-container');
  if (!cmds.length) { c.innerHTML = ''; return; }
  c.innerHTML = cmds.map((cmd, i) => `
    <div class="list-row" id="bc-row-${i}">
      <input class="form-control form-control-sm font-monospace" placeholder="npm install"
        value="${esc(cmd)}" oninput="updateBuildCommand(${i},this.value)">
      <button class="btn-icon" onclick="removeBuildCommand(${i})" title="Remove">✕</button>
    </div>`).join('');
}

function addBuildCommand() {
  ensureProvider();
  currentProvider.build_commands = currentProvider.build_commands || [];
  currentProvider.build_commands.push('');
  renderBuildCommands(currentProvider.build_commands);
}

function removeBuildCommand(i) {
  ensureProvider();
  currentProvider.build_commands.splice(i, 1);
  renderBuildCommands(currentProvider.build_commands);
}

function updateBuildCommand(i, val) {
  ensureProvider();
  currentProvider.build_commands[i] = val;
}

function updateRepoField(key, val) {
  ensureProvider();
  currentProvider[key] = val;
}

// Env keys list (repository providers)
function renderEnvKeys(keys) {
  const c = document.getElementById('env-keys-container');
  if (!keys.length) { c.innerHTML = '<div class="text-muted" style="font-size:.8em">(none discovered — no .env.example in the repo, or repo not built yet)</div>'; return; }
  c.innerHTML = keys.map((k, i) => `
    <div class="list-row" id="ek-row-${i}">
      <input class="form-control form-control-sm font-monospace" placeholder="MY_API_KEY"
        value="${esc(k)}" oninput="updateEnvKey(${i},this.value)">
      <button class="btn-icon" onclick="removeEnvKey(${i})" title="Remove">✕</button>
    </div>`).join('');
}

function addEnvKey() {
  ensureProvider();
  currentProvider.repo_env_keys = currentProvider.repo_env_keys || [];
  currentProvider.repo_env_keys.push('');
  renderEnvKeys(currentProvider.repo_env_keys);
}

function removeEnvKey(i) {
  ensureProvider();
  currentProvider.repo_env_keys.splice(i, 1);
  renderEnvKeys(currentProvider.repo_env_keys);
}

function updateEnvKey(i, val) {
  ensureProvider();
  currentProvider.repo_env_keys[i] = val.trim();
}

async function rescanEnvExample() {
  if (!currentProvider || currentProvider.type !== 'repository') return;
  const wd = currentProvider.workdir;
  if (!wd) { toast('No workdir — click Re-clone & build first', false); return; }
  try {
    const r = await api('POST', '/api/scan-env-example', {workdir: wd});
    if (!r.ok) throw new Error(r.error || 'scan failed');
    // Merge: preserve any keys the user typed manually, add new ones from .env.example
    const existing = new Set(currentProvider.repo_env_keys || []);
    (r.env_keys || []).forEach(k => existing.add(k));
    currentProvider.repo_env_keys = Array.from(existing);
    renderEnvKeys(currentProvider.repo_env_keys);
    toast(`Discovered ${r.env_keys.length} env key(s)`);
  } catch (e) { toast(e.message, false); }
}

async function rebuildRepository() {
  if (!currentProvider || currentProvider.type !== 'repository') return;
  const el = document.getElementById('rebuild-status');
  el.className = 'fn-status busy';
  el.textContent = 'Cloning / pulling and running build commands…';
  try {
    const r = await api('POST', '/api/clone-and-build', {
      name: currentName,
      repo_url: currentProvider.repo_url,
      ref: currentProvider.repo_ref,
      build_commands: currentProvider.build_commands || [],
      workdir: currentProvider.workdir || '',
    });
    currentProvider.workdir = r.workdir || currentProvider.workdir;
    document.getElementById('f-repo-workdir').textContent = currentProvider.workdir;
    // Merge discovered env_keys with whatever the user already has
    if (r.env_keys && r.env_keys.length) {
      const existing = new Set(currentProvider.repo_env_keys || []);
      r.env_keys.forEach(k => existing.add(k));
      currentProvider.repo_env_keys = Array.from(existing);
      renderEnvKeys(currentProvider.repo_env_keys);
    }
    if (!r.ok) {
      el.className = 'fn-status error';
      el.textContent = (r.error || 'build failed') + (r.failed_command ? ` (running ${r.failed_command})` : '');
      return;
    }
    el.className = 'fn-status ok';
    el.textContent = `Built ✓ (workdir: ${r.workdir})`;
    discoverFunctions().catch(() => {});
  } catch (e) {
    el.className = 'fn-status error';
    el.textContent = e.message || 'build failed';
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Requirements and Setup Commands rendering
// ─────────────────────────────────────────────────────────────────────────────
function renderRequirements(reqs) {
  const c = document.getElementById('requirements-container');
  if (!reqs.length) { c.innerHTML = ''; return; }
  c.innerHTML = reqs.map((r, i) => `
    <div class="list-row" id="req-row-${i}">
      <input class="form-control form-control-sm font-monospace" placeholder="package-name==1.2.3"
        value="${esc(r)}" oninput="updateRequirement(${i},this.value)">
      <button class="btn-icon" onclick="removeRequirement(${i})" title="Remove">✕</button>
    </div>`).join('');
}

function renderSetupCommands(cmds) {
  const c = document.getElementById('setup-commands-container');
  if (!cmds.length) { c.innerHTML = ''; return; }
  c.innerHTML = cmds.map((cmd, i) => `
    <div class="list-row" id="sc-row-${i}">
      <input class="form-control form-control-sm font-monospace" placeholder="npx playwright install chrome"
        value="${esc(cmd)}" oninput="updateSetupCommand(${i},this.value)">
      <button class="btn-icon" onclick="removeSetupCommand(${i})" title="Remove">✕</button>
    </div>`).join('');
}

function addRequirement() {
  ensureProvider();
  currentProvider.requirements = currentProvider.requirements || [];
  currentProvider.requirements.push('');
  renderRequirements(currentProvider.requirements);
}

function removeRequirement(i) {
  ensureProvider();
  currentProvider.requirements.splice(i, 1);
  renderRequirements(currentProvider.requirements);
}

function updateRequirement(i, val) {
  ensureProvider();
  currentProvider.requirements[i] = val;
}

function addSetupCommand() {
  ensureProvider();
  currentProvider.setup_commands = currentProvider.setup_commands || [];
  currentProvider.setup_commands.push('');
  renderSetupCommands(currentProvider.setup_commands);
}

function removeSetupCommand(i) {
  ensureProvider();
  currentProvider.setup_commands.splice(i, 1);
  renderSetupCommands(currentProvider.setup_commands);
}

function updateSetupCommand(i, val) {
  ensureProvider();
  currentProvider.setup_commands[i] = val;
}

// ─────────────────────────────────────────────────────────────────────────────
// Tools rendering
// ─────────────────────────────────────────────────────────────────────────────
function renderTools(tools, isPkg) {
  const container = document.getElementById('tools-container');
  if (!tools.length) {
    container.innerHTML = '<div class="empty-state" style="padding:16px">No tools yet — click <b>+ Add Tool</b>.</div>';
    return;
  }
  container.innerHTML = tools.map((t, i) => renderToolCard(t, i, isPkg)).join('');
}

function renderToolCard(t, i, isPkg) {
  const enabled = t.enabled !== false;
  const params = (t.parameters || []).map((p, j) => `
    <div class="param-row" id="param-${i}-${j}">
      <input class="form-control form-control-sm" placeholder="name" value="${esc(p.name)}"
        oninput="updateParam(${i},${j},'name',this.value)">
      <select class="form-select form-select-sm" onchange="updateParam(${i},${j},'type',this.value)">
        ${['string','integer','number','boolean','object','array'].map(ty =>
          `<option ${p.type===ty?'selected':''}>${ty}</option>`).join('')}
      </select>
      <input class="form-control form-control-sm" placeholder="description" value="${esc(p.description||'')}"
        oninput="updateParam(${i},${j},'description',this.value)">
      <div class="d-flex align-items-center gap-1">
        <label class="d-flex align-items-center gap-1 mb-0" style="cursor:pointer;white-space:nowrap">
          <input type="checkbox" ${p.required?'checked':''} onchange="updateParam(${i},${j},'required',this.checked)">
          <span style="font-size:.75em;color:var(--muted)">req</span>
        </label>
        <button class="btn-icon" onclick="removeParam(${i},${j})" title="Remove">✕</button>
      </div>
    </div>`).join('');

  const secrets = (t.secrets || []).map((s, k) => `
    <div class="secret-row" id="secret-${i}-${k}">
      <input class="form-control form-control-sm font-monospace" placeholder="handler arg" value="${esc(s.arg||'')}"
        oninput="updateSecret(${i},${k},'arg',this.value)">
      <input class="form-control form-control-sm font-monospace" placeholder="ENV_VAR_NAME" value="${esc(s.env||'')}"
        oninput="updateSecret(${i},${k},'env',this.value)">
      <button class="btn-icon" onclick="removeSecret(${i},${k})" title="Remove">✕</button>
    </div>`).join('');

  // Function/name picker: a dropdown of known names + "Other…" alongside the
  // text input.  Picking a menu option fills the input; the input is the
  // source of truth so users can always free-type when "Other…" is chosen
  // or the upstream list is unknown.
  const nameField = `
    <div class="mb-2">
      <label class="form-label">Tool name${isPkg ? ' <span class="text-muted fw-normal" style="text-transform:none">— must match upstream</span>' : ''}</label>
      <div class="fn-pick-row">
        ${isPkg ? `
          <select class="form-select form-select-sm" id="fn-pick-${i}"
            onchange="onFnPick(${i}, 'name', this.value)">
            ${_fnPickOptionsHtml(t.name || '')}
          </select>` : ''}
        <input class="form-control form-control-sm font-monospace" id="tool-name-input-${i}"
          placeholder="my_tool" value="${esc(t.name||'')}"
          oninput="updateTool(${i},'name',this.value);_syncFnPick(${i},'name',this.value);document.getElementById('tool-label-${i}').textContent=this.value||'(unnamed)'">
      </div>
    </div>`;

  const fnField = isPkg ? '' : `
    <div class="mb-2">
      <label class="form-label">Function name</label>
      <div class="fn-pick-row">
        <select class="form-select form-select-sm" id="fn-pick-${i}"
          onchange="onFnPick(${i}, 'function', this.value)">
          ${_fnPickOptionsHtml(t.function || '')}
        </select>
        <input class="form-control form-control-sm font-monospace" id="tool-function-input-${i}"
          placeholder="my_function" value="${esc(t.function||'')}"
          oninput="updateTool(${i},'function',this.value);_syncFnPick(${i},'function',this.value)">
      </div>
    </div>`;

  const docField = `
    <div class="mb-2">
      <label class="form-label">Documentation <span class="text-muted fw-normal" style="text-transform:none">optional</span></label>
      <textarea class="form-control form-control-sm" rows="2" placeholder="Per-tool usage notes…"
        oninput="updateTool(${i},'documentation',this.value)">${esc(t.documentation||'')}</textarea>
    </div>`;

  return `
  <div class="tool-card ${enabled ? '' : 'disabled'}" id="tool-card-${i}">
    <div class="tool-card-header" onclick="toggleToolCard(${i})">
      <div class="d-flex gap-2 align-items-center" style="min-width:0">
        <label class="form-check form-switch m-0" onclick="event.stopPropagation()" title="When unchecked the tool is kept in YAML but not advertised to the LLM.">
          <input class="form-check-input" type="checkbox" ${enabled?'checked':''}
            onchange="setToolEnabled(${i}, this.checked)">
        </label>
        <span class="fw-semibold" id="tool-label-${i}" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(t.name||'(unnamed)')}</span>
        <span class="badge-disabled" id="tool-disabled-badge-${i}" style="${enabled?'display:none':''}">disabled</span>
      </div>
      <div class="d-flex gap-2 align-items-center" onclick="event.stopPropagation()">
        <button class="btn btn-sm btn-outline-danger py-0 px-2" onclick="removeTool(${i})">Remove</button>
        <span style="color:var(--muted)">▾</span>
      </div>
    </div>
    <div class="tool-card-body" id="tool-body-${i}">
      <div class="row g-2 mb-2">
        <div class="col-md-5">
          ${nameField}
        </div>
        <div class="col-md-7">
          <label class="form-label">Description <span style="color:var(--red)">*</span></label>
          <input class="form-control form-control-sm" placeholder="What this tool does…" value="${esc(t.description||'')}"
            oninput="updateTool(${i},'description',this.value)">
        </div>
      </div>
      ${fnField}
      ${docField}
      <div class="section-title mt-3">Parameters <button class="btn btn-sm btn-outline-secondary py-0" onclick="addParam(${i})">+ Add</button></div>
      <div style="font-size:.75em;color:var(--muted);margin-bottom:6px">name &nbsp;·&nbsp; type &nbsp;·&nbsp; description &nbsp;·&nbsp; required</div>
      <div id="params-${i}">${params || '<div class="text-muted" style="font-size:.8em;padding:4px">No parameters.</div>'}</div>
      <div class="section-title mt-3">Secrets <button class="btn btn-sm btn-outline-secondary py-0" onclick="addSecret(${i})">+ Add</button></div>
      <div style="font-size:.75em;color:var(--muted);margin-bottom:6px">handler arg &nbsp;→&nbsp; ENV_VAR_NAME (value injected server-side, never in LLM schema)</div>
      <div id="secrets-${i}">${secrets || '<div class="text-muted" style="font-size:.8em;padding:4px">No secrets.</div>'}</div>
    </div>
  </div>`;
}

// Dropdown picked — fill the matching text input.  An empty value ("— pick
// from menu —") leaves the input alone; "__other__" clears it and focuses it.
function onFnPick(i, field, value) {
  ensureProvider();
  if (value === '') return;
  if (value === '__other__') {
    const input = document.getElementById(`tool-${field}-input-${i}`);
    if (input) { input.value = ''; input.focus(); }
    updateTool(i, field, '');
    if (field === 'name') document.getElementById(`tool-label-${i}`).textContent = '(unnamed)';
    return;
  }
  const input = document.getElementById(`tool-${field}-input-${i}`);
  if (input) input.value = value;
  updateTool(i, field, value);
  if (field === 'name') document.getElementById(`tool-label-${i}`).textContent = value;
}

// Keep the dropdown's selection in sync when the user types in the input.
function _syncFnPick(i, field, value) {
  const sel = document.getElementById(`fn-pick-${i}`);
  if (!sel) return;
  if (knownFunctions.includes(value)) {
    sel.value = value;
  } else {
    sel.value = '';  // "— pick from menu —"
  }
}

function setToolEnabled(i, enabled) {
  ensureProvider();
  currentProvider.tools[i].enabled = !!enabled;
  const card = document.getElementById(`tool-card-${i}`);
  if (card) card.classList.toggle('disabled', !enabled);
  const badge = document.getElementById(`tool-disabled-badge-${i}`);
  if (badge) badge.style.display = enabled ? 'none' : '';
}

function toggleToolCard(i) {
  const body = document.getElementById(`tool-body-${i}`);
  body.style.display = body.style.display === 'none' ? '' : 'none';
}

// ─────────────────────────────────────────────────────────────────────────────
// Provider mutations (operate on currentProvider in memory, re-render on save)
// ─────────────────────────────────────────────────────────────────────────────
function ensureProvider() {
  if (!currentProvider) throw new Error('No provider loaded');
}

function collectProvider() {
  const p = JSON.parse(JSON.stringify(currentProvider));
  p.documentation = document.getElementById('f-documentation').value;
  if (p.type === 'repository') {
    p.command = document.getElementById('f-command').value.trim();
    p.repo_url = document.getElementById('f-repo-url').value.trim();
    p.repo_ref = document.getElementById('f-repo-ref').value.trim();
    p.build_commands = (currentProvider.build_commands || []).filter(c => c.trim());
    p.repo_env_keys = (currentProvider.repo_env_keys || []).filter(k => k.trim());
  } else if (p.type === 'package') {
    p.command = document.getElementById('f-command').value.trim();
  } else if (p.type === 'rest') {
    // auth + endpoints are carried in currentProvider.rest; base URL comes from
    // the field, and the header rows are serialized back into a plain object.
    p.rest = currentProvider.rest || {};
    p.rest.base_url = document.getElementById('f-rest-base-url').value.trim();
    const headers = {};
    (p.rest.headerRows || []).forEach(h => { if (h.key && h.key.trim()) headers[h.key.trim()] = h.value; });
    p.rest.headers = headers;
    delete p.rest.headerRows;
  } else {
    p.code = codeEditor.getValue();
  }
  // requirements and setup_commands are kept in currentProvider (updated live via oninput)
  p.requirements = (currentProvider.requirements || []).filter(r => r.trim());
  p.setup_commands = (currentProvider.setup_commands || []).filter(c => c.trim());
  return p;
}

function updateTool(i, field, val) {
  ensureProvider();
  currentProvider.tools[i][field] = val;
}

function updateParam(ti, pi, field, val) {
  ensureProvider();
  currentProvider.tools[ti].parameters[pi][field] = val;
}

function updateSecret(ti, si, field, val) {
  ensureProvider();
  currentProvider.tools[ti].secrets[si][field] = val;
}

function addTool() {
  ensureProvider();
  const isPkg = currentProvider.type === 'package' || currentProvider.type === 'repository' || currentProvider.type === 'rest';
  currentProvider.tools.push({
    name: '', function: '', description: '', documentation: '',
    enabled: true, parameters: [], secrets: [],
  });
  renderTools(currentProvider.tools, isPkg);
}

function removeTool(i) {
  ensureProvider();
  currentProvider.tools.splice(i, 1);
  renderTools(currentProvider.tools, currentProvider.type === 'package' || currentProvider.type === 'repository' || currentProvider.type === 'rest');
}

function addParam(ti) {
  ensureProvider();
  currentProvider.tools[ti].parameters.push({name:'',type:'string',description:'',required:false,default:null});
  renderTools(currentProvider.tools, currentProvider.type === 'package' || currentProvider.type === 'repository' || currentProvider.type === 'rest');
}

function removeParam(ti, pi) {
  ensureProvider();
  currentProvider.tools[ti].parameters.splice(pi, 1);
  renderTools(currentProvider.tools, currentProvider.type === 'package' || currentProvider.type === 'repository' || currentProvider.type === 'rest');
}

function addSecret(ti) {
  ensureProvider();
  currentProvider.tools[ti].secrets.push({arg:'',env:''});
  renderTools(currentProvider.tools, currentProvider.type === 'package' || currentProvider.type === 'repository' || currentProvider.type === 'rest');
}

function removeSecret(ti, si) {
  ensureProvider();
  currentProvider.tools[ti].secrets.splice(si, 1);
  renderTools(currentProvider.tools, currentProvider.type === 'package' || currentProvider.type === 'repository' || currentProvider.type === 'rest');
}

// ─────────────────────────────────────────────────────────────────────────────
// Save / Delete / Restart
// ─────────────────────────────────────────────────────────────────────────────
async function saveProvider() {
  if (!currentName) return;
  const provider = collectProvider();
  try {
    const r = await api('PUT', `/api/tools/${currentName}`, {provider});
    toast('Saved ✓');
    currentProvider = provider;
    document.getElementById('restart-bar').style.display = '';
    await loadList();
    _refreshEditorBars(currentName);
  } catch(e) { toast(e.message, false); }
}

async function deleteProvider() {
  if (!currentName || !confirm(`Delete ${currentName}?`)) return;
  try {
    await api('DELETE', `/api/tools/${currentName}`);
    currentName = null; currentProvider = null;
    document.getElementById('empty-panel').style.display = '';
    document.getElementById('editor-panel').style.display = 'none';
    toast('Deleted ✓');
    loadList();
  } catch(e) { toast(e.message, false); }
}

async function restartServer() {
  try { await api('POST', '/api/restart'); toast('Restart signal sent — reconnect in a moment'); }
  catch(e) { toast('Signal sent', true); }
}

// ─────────────────────────────────────────────────────────────────────────────
// Interactive web terminal (xterm.js ↔ /ws/terminal PTY bridge)
// ─────────────────────────────────────────────────────────────────────────────
function openTerminal(cmd, title) {
  if (!webTerminalEnabled) { toast('Web terminal is disabled (MCPPROXY_WEB_TERMINAL=0)', false); return; }
  document.getElementById('terminal-title').textContent = title || 'Terminal';
  termModal.show();
  // Defer until the modal is laid out so fit() measures real dimensions.
  setTimeout(() => startTerminal(cmd), 250);
}

function startTerminal(cmd) {
  closeTerminal();
  const host = document.getElementById('terminal-host');
  host.innerHTML = '';
  term = new Terminal({fontSize: 13, fontFamily: "'JetBrains Mono',Consolas,monospace",
    theme: {background: '#000000'}, cursorBlink: true, convertEol: true});
  termFit = new FitAddon.FitAddon();
  term.loadAddon(termFit);
  term.open(host);
  try { termFit.fit(); } catch {}

  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  let url = `${proto}://${location.host}/ws/terminal`;
  if (cmd) url += '?cmd=' + encodeURIComponent(cmd);
  termSock = new WebSocket(url);
  termSock.binaryType = 'arraybuffer';
  const dec = new TextDecoder();
  termSock.onopen = () => sendResize();
  termSock.onmessage = ev => {
    term.write(typeof ev.data === 'string' ? ev.data : dec.decode(new Uint8Array(ev.data)));
  };
  termSock.onclose = () => { if (term) term.write('\r\n\x1b[33m[session ended]\x1b[0m\r\n'); };
  term.onData(d => { if (termSock && termSock.readyState === 1) termSock.send(JSON.stringify({input: d})); });
  term.onResize(() => sendResize());
  window.addEventListener('resize', _termResize);
}

function sendResize() {
  if (!term || !termSock || termSock.readyState !== 1) return;
  try { termFit.fit(); } catch {}
  termSock.send(JSON.stringify({resize: [term.cols, term.rows]}));
}
function _termResize() { try { termFit && termFit.fit(); sendResize(); } catch {} }

function closeTerminal() {
  window.removeEventListener('resize', _termResize);
  if (termSock) { try { termSock.close(); } catch {} termSock = null; }
  if (term) { try { term.dispose(); } catch {} term = null; }
  termFit = null;
  pollPendingAuth();  // the bootstrap may have just cleared a pending auth
}

// Re-run the mcp-remote OAuth flow for the currently-open provider.
function reauthorizeProvider() {
  const cmd = (document.getElementById('f-command').value || '').trim();
  if (!cmd) { toast('No command to re-authorize', false); return; }
  openTerminal(cmd, `Re-authorize ${currentName || ''}`);
}

// Wizard: bootstrap the remote provider's token cache from a terminal.
function wzBootstrapRemote() {
  const url = (document.getElementById('wz-remote-url').value || '').trim();
  if (!/^https?:\/\//i.test(url)) {
    document.getElementById('wz-error').textContent = 'Enter a server URL first.'; return;
  }
  openTerminal('npx -y mcp-remote ' + url, 'Bootstrap ' + url);
}

// ─────────────────────────────────────────────────────────────────────────────
// Secrets modal
// ─────────────────────────────────────────────────────────────────────────────
async function openSecretsModal() {
  if (!currentProvider) return;
  document.getElementById('secrets-provider-name').textContent = currentName;
  const env = await api('GET', '/api/env').catch(() => ({vars:{}, env_file:'.env'}));
  document.getElementById('secrets-env-path').textContent = env.env_file || '.env';
  const keys = [];
  for (const t of currentProvider.tools) {
    for (const s of (t.secrets || [])) {
      if (s.env && !keys.includes(s.env)) keys.push(s.env);
    }
  }
  const existing = env.vars || {};
  const el = document.getElementById('secrets-fields');
  if (!keys.length) {
    el.innerHTML = '<div class="text-muted">No secrets declared in this provider.</div>';
  } else {
    el.innerHTML = keys.map(k => {
      const isSet = !!existing[k];
      return `<div class="section-box ${isSet?'secret-set':'secret-unset'} mb-2">
        <div class="d-flex align-items-center gap-2 mb-1">
          <span class="fw-semibold font-monospace" style="font-size:.9em">${k}</span>
          ${isSet ? '<span style="color:var(--green);font-size:.8em">✓ set</span>' : '<span style="color:var(--yellow);font-size:.8em">not set</span>'}
        </div>
        <input class="form-control form-control-sm" type="password" id="secret-${k}"
          placeholder="${isSet ? 'leave blank to keep existing' : 'enter value…'}">
      </div>`;
    }).join('');
  }
  secretsModal.show();
}

async function saveSecrets() {
  const inputs = document.querySelectorAll('#secrets-fields input[type=password]');
  const vars = {};
  inputs.forEach(el => {
    const key = el.id.replace('secret-', '');
    if (el.value.trim()) vars[key] = el.value.trim();
  });
  if (!Object.keys(vars).length) { secretsModal.hide(); return; }
  try {
    await api('POST', '/api/env', {vars});
    toast(`Saved ${Object.keys(vars).length} secret(s) ✓`);
    secretsModal.hide();
    await loadList();
    if (currentName) _refreshEditorBars(currentName);
  } catch(e) { toast(e.message, false); }
}

// ─────────────────────────────────────────────────────────────────────────────
// Wizard
// ─────────────────────────────────────────────────────────────────────────────
const WZ_STEPS = ['type','remote','package','repository','rest','code','secrets'];

function wzShowStep(step) {
  WZ_STEPS.forEach(s => {
    const el = document.getElementById(`wz-${s}`);
    if (el) el.classList.toggle('active', s === step);
  });
  wzStep = step;
  document.getElementById('wz-back-btn').style.display = step === 'type' ? 'none' : '';
  document.getElementById('wz-next-btn').textContent = step === 'secrets' ? 'Finish' : 'Next →';
  document.getElementById('wz-error').textContent = '';
  // The terminal-bootstrap shortcut only makes sense on the remote step when
  // the web terminal feature is enabled.
  const bootstrapWrap = document.getElementById('wz-remote-bootstrap-wrap');
  if (bootstrapWrap) bootstrapWrap.style.display = (step === 'remote' && webTerminalEnabled) ? '' : 'none';
}

function openWizard() {
  wzType = null; wzStep = 'type'; wzIntrospectedTools = []; wzRepoCtx = null;
  document.getElementById('wz-pkg-name').value = '';
  document.getElementById('wz-pkg-cmd').value = '';
  document.getElementById('wz-pkg-reqs-container').innerHTML = '';
  document.getElementById('wz-pkg-cmds-container').innerHTML = '';
  document.getElementById('wz-code-name').value = '';
  document.getElementById('wz-code-input').value = '';
  document.getElementById('wz-code-reqs-container').innerHTML = '';
  document.getElementById('wz-code-cmds-container').innerHTML = '';
  document.getElementById('wz-introspect-result').innerHTML = '';
  document.getElementById('wz-analyze-result').innerHTML = '';
  document.getElementById('wz-repo-name').value = '';
  document.getElementById('wz-repo-url').value = '';
  document.getElementById('wz-repo-ref').value = '';
  document.getElementById('wz-repo-cmd').value = '';
  document.getElementById('wz-repo-builds-container').innerHTML = '';
  document.getElementById('wz-repo-result').innerHTML = '';
  document.getElementById('wz-remote-name').value = '';
  document.getElementById('wz-remote-url').value = '';
  document.getElementById('wz-remote-result').innerHTML = '';
  wzRestReset();
  wzShowStep('type');
  wizModal.show();
}

function wzRestReset() {
  wzRestEndpoints = [];
  wzRestEndpointTools = {};
  ['wz-rest-name','wz-rest-base-url','wz-rest-token-env','wz-rest-header','wz-rest-value-env',
   'wz-rest-authorize-url','wz-rest-token-url','wz-rest-client-id-env','wz-rest-client-secret-env',
   'wz-rest-scopes','wz-rest-openapi'].forEach(id => { const el = document.getElementById(id); if (el) el.value = ''; });
  const at = document.getElementById('wz-rest-auth-type'); if (at) at.value = 'none';
  const ec = document.getElementById('wz-rest-endpoints-container'); if (ec) ec.innerHTML = '';
  const res = document.getElementById('wz-rest-result'); if (res) res.innerHTML = '';
  wzRestAuthChanged();
  wzRestTab('openapi');
  const ru = document.getElementById('wz-rest-redirect-uri');
  if (ru) ru.textContent = (window.location.origin || 'http://localhost:8889') + '/oauth/callback';
}

function wzAddRepoBuild() { _wzListAdd('wz-repo-builds-container', 'npm install'); }

// One-click Node/TypeScript defaults — covers the common case (e.g. the
// linkedin-mcpserver / typical fastmcp-style TS repos).
function wzPrefillRepoNodeTs() {
  const container = document.getElementById('wz-repo-builds-container');
  container.innerHTML = '';
  _wzListAdd('wz-repo-builds-container', 'npm install');
  _wzListAdd('wz-repo-builds-container', 'npm run build');
  // Populate the two input slots we just appended
  const inputs = container.querySelectorAll('input');
  if (inputs.length >= 2) {
    inputs[0].value = 'npm install';
    inputs[1].value = 'npm run build';
  }
  document.getElementById('wz-repo-cmd').value = 'node build/index.js';
}

function wzSelectType(type, prefill) {
  wzType = type;
  document.querySelectorAll('.wizard-choice').forEach(el => el.classList.remove('selected'));
  // Highlight the clicked card when invoked from an onclick handler; when called
  // programmatically (e.g. from the catalog) there is no matching card event.
  const ct = window.event && window.event.currentTarget;
  if (ct && ct.classList && ct.classList.contains('wizard-choice')) ct.classList.add('selected');
  setTimeout(() => {
    wzShowStep(type);
    if (typeof prefill === 'function') prefill();
  }, 120);
}

// ─────────────────────────────────────────────────────────────────────────────
// Browse providers catalog
// ─────────────────────────────────────────────────────────────────────────────
function _slugify(s) {
  return (String(s||'').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '')) || 'provider';
}

function openCatalog() {
  catalogModal = catalogModal || new bootstrap.Modal(document.getElementById('catalog-modal'));
  catalogModal.show();
  loadCatalog(document.getElementById('catalog-live').checked);
}

async function loadCatalog(live) {
  const list = document.getElementById('catalog-list');
  list.innerHTML = '<div class="empty-state">Loading…</div>';
  try {
    const data = await api('GET', '/api/catalog?live=' + (live ? 'true' : 'false'));
    catalogEntries = data.entries || [];
    renderCatalog();
    const errs = Object.keys(data.errors || {});
    if (errs.length) toast('Some registries were unavailable: ' + errs.join(', '), false);
  } catch (e) {
    list.innerHTML = '<div class="empty-state" style="color:var(--red)">' + esc(e.message) + '</div>';
  }
}

function catalogSearch() { renderCatalog(); }

function catalogToggleLive(cb) { loadCatalog(cb.checked); }

function renderCatalog() {
  const q = (document.getElementById('catalog-search').value || '').toLowerCase().trim();
  const kind = document.getElementById('catalog-kind').value;
  const list = document.getElementById('catalog-list');
  const rows = catalogEntries.filter(e => {
    if (kind && e.kind !== kind) return false;
    if (!q) return true;
    return (e.name + ' ' + (e.description || '') + ' ' + (e.categories || []).join(' ')).toLowerCase().includes(q);
  });
  if (!rows.length) { list.innerHTML = '<div class="empty-state">No matching providers.</div>'; return; }
  list.innerHTML = rows.map((e, i) => {
    const badge = e.kind === 'mcp_remote'
      ? '<span class="badge bg-info text-dark">MCP server</span>'
      : '<span class="badge bg-secondary">REST / OpenAPI</span>';
    const src = e.source && e.source !== 'curated'
      ? '<span class="badge bg-dark border" style="font-weight:500">' + esc(e.source) + '</span>' : '';
    const home = e.homepage
      ? ' <a href="' + esc(e.homepage) + '" target="_blank" rel="noopener" style="font-size:.85em">↗</a>' : '';
    return '<div class="card mb-2"><div class="card-body py-2 d-flex align-items-center gap-3">'
      + '<div class="flex-grow-1" style="min-width:0">'
      + '<div class="d-flex align-items-center gap-2 flex-wrap">'
      + '<strong>' + esc(e.name) + '</strong>' + badge + src + home + '</div>'
      + '<div class="text-muted" style="font-size:.85em">' + esc(e.description || '') + '</div>'
      + '</div>'
      + '<button class="btn btn-sm btn-success flex-shrink-0" onclick="catalogConfigure(' + i + ')">Configure →</button>'
      + '</div></div>';
  }).join('');
  // Stash the filtered rows so the Configure buttons resolve by index.
  list._rows = rows;
}

function catalogConfigure(idx) {
  const entry = (document.getElementById('catalog-list')._rows || [])[idx];
  if (!entry) return;
  const el = document.getElementById('catalog-modal');
  // Wait for the catalog modal to finish closing before opening the wizard so
  // Bootstrap doesn't leave a stale backdrop behind.
  el.addEventListener('hidden.bs.modal', () => _wizardFromEntry(entry), { once: true });
  catalogModal.hide();
}

function _wizardFromEntry(entry) {
  openWizard();
  if (entry.kind === 'mcp_remote') {
    wzSelectType('remote', () => {
      document.getElementById('wz-remote-name').value = entry.id || _slugify(entry.name);
      document.getElementById('wz-remote-url').value = entry.url || '';
    });
  } else if (entry.kind === 'rest_openapi') {
    wzSelectType('rest', () => {
      document.getElementById('wz-rest-name').value = entry.id || _slugify(entry.name);
      document.getElementById('wz-rest-base-url').value = entry.base_url || '';
      document.getElementById('wz-rest-openapi').value = entry.openapi_url || '';
      if (entry.auth_hint) {
        const sel = document.getElementById('wz-rest-auth-type');
        if (sel && [...sel.options].some(o => o.value === entry.auth_hint)) {
          sel.value = entry.auth_hint;
          wzRestAuthChanged();
        }
      }
      wzRestTab('openapi');
    });
  }
}

// ── REST wizard helpers ──────────────────────────────────────────────────────

function wzRestAuthChanged() {
  const type = document.getElementById('wz-rest-auth-type').value;
  const wrap = document.getElementById('wz-rest-auth-fields');
  wrap.style.display = type === 'none' ? 'none' : '';
  document.querySelectorAll('#wz-rest-auth-fields .wz-rest-auth').forEach(el => el.style.display = 'none');
  if (type === 'bearer') document.querySelector('.wz-rest-auth-bearer').style.display = '';
  else if (type === 'api_key') document.querySelector('.wz-rest-auth-api_key').style.display = '';
  else if (type === 'client_credentials' || type === 'authorization_code') {
    document.querySelector('.wz-rest-auth-oauth').style.display = '';
    const authCodeOnly = type === 'authorization_code';
    document.querySelectorAll('.wz-rest-auth-authcode-only').forEach(el => el.style.display = authCodeOnly ? '' : 'none');
    document.getElementById('wz-rest-secret-optional').textContent = authCodeOnly ? 'optional (PKCE)' : 'required';
  }
}

function wzRestTab(which) {
  const openapi = which === 'openapi';
  document.getElementById('wz-rest-tab-openapi').classList.toggle('active', openapi);
  document.getElementById('wz-rest-tab-manual').classList.toggle('active', !openapi);
  document.getElementById('wz-rest-openapi-pane').style.display = openapi ? '' : 'none';
  document.getElementById('wz-rest-manual-pane').style.display = openapi ? 'none' : '';
}

function wzRestCollectAuth() {
  const type = document.getElementById('wz-rest-auth-type').value;
  const auth = { type };
  const g = id => (document.getElementById(id).value || '').trim();
  if (type === 'bearer') auth.token_env = g('wz-rest-token-env');
  else if (type === 'api_key') { auth.header = g('wz-rest-header') || 'X-Api-Key'; auth.value_env = g('wz-rest-value-env'); }
  else if (type === 'client_credentials' || type === 'authorization_code') {
    auth.token_url = g('wz-rest-token-url');
    auth.client_id_env = g('wz-rest-client-id-env');
    const sec = g('wz-rest-client-secret-env'); if (sec) auth.client_secret_env = sec;
    const scopes = g('wz-rest-scopes'); if (scopes) auth.scopes = scopes.split(/\s+/).filter(Boolean);
    if (type === 'authorization_code') auth.authorize_url = g('wz-rest-authorize-url');
  }
  return auth;
}

function wzRestValidateAuth(auth) {
  if (auth.type === 'bearer' && !auth.token_env) return 'Bearer auth needs a token env var.';
  if (auth.type === 'api_key' && !auth.value_env) return 'API-key auth needs a value env var.';
  if (auth.type === 'client_credentials') {
    if (!auth.token_url || !auth.client_id_env || !auth.client_secret_env)
      return 'Client-credentials needs token URL, client ID env, and client secret env.';
  }
  if (auth.type === 'authorization_code') {
    if (!auth.authorize_url || !auth.token_url || !auth.client_id_env)
      return 'Authorization-code needs authorize URL, token URL, and client ID env.';
  }
  return '';
}

async function wzRestIntrospect() {
  const source = document.getElementById('wz-rest-openapi').value.trim();
  const resEl = document.getElementById('wz-rest-result');
  if (!source) { resEl.innerHTML = '<span class="text-danger" style="font-size:.85em">Enter an OpenAPI URL or file path first.</span>'; return; }
  resEl.innerHTML = '<span class="text-muted" style="font-size:.85em">Parsing OpenAPI spec…</span>';
  try {
    const r = await api('POST', '/api/introspect-openapi', {openapi: source});
    if (!r.ok) throw new Error(r.error || 'introspection failed');
    wzRestEndpoints = r.endpoints || [];
    wzRestEndpointTools = {};
    (r.tools || []).forEach(t => { wzRestEndpointTools[t.name] = t; });
    resEl.innerHTML = `<div style="color:var(--green);font-size:.85em">✓ Found ${wzRestEndpoints.length} endpoint(s)</div>`;
  } catch(e) {
    wzRestEndpoints = []; wzRestEndpointTools = {};
    resEl.innerHTML = `<div class="text-danger" style="font-size:.85em">✗ ${esc(e.message)}</div>`;
  }
}

function wzRestAddEndpoint() {
  const c = document.getElementById('wz-rest-endpoints-container');
  const idx = c.children.length;
  const div = document.createElement('div');
  div.className = 'border rounded p-2 mt-1';
  div.innerHTML = `
    <div class="row g-2">
      <div class="col-4"><input class="form-control form-control-sm font-monospace wz-ep-name" placeholder="get_user"></div>
      <div class="col-2">
        <select class="form-select form-select-sm wz-ep-method">
          <option>GET</option><option>POST</option><option>PUT</option><option>PATCH</option><option>DELETE</option>
        </select>
      </div>
      <div class="col-5"><input class="form-control form-control-sm font-monospace wz-ep-path" placeholder="/users/{user_id}"></div>
      <div class="col-1"><button class="btn-icon" onclick="this.closest('.border').remove()" title="Remove">✕</button></div>
    </div>
    <div class="row g-2 mt-1" style="font-size:.8em">
      <div class="col"><input class="form-control form-control-sm font-monospace wz-ep-pathp" placeholder="path params: user_id"></div>
      <div class="col"><input class="form-control form-control-sm font-monospace wz-ep-queryp" placeholder="query params: include"></div>
      <div class="col"><input class="form-control form-control-sm font-monospace wz-ep-bodyp" placeholder="body params: title,body"></div>
    </div>`;
  c.appendChild(div);
}

function wzRestCollectManualEndpoints() {
  const eps = [];
  const tools = {};
  document.querySelectorAll('#wz-rest-endpoints-container > .border').forEach(div => {
    const name = (div.querySelector('.wz-ep-name').value || '').trim();
    const path = (div.querySelector('.wz-ep-path').value || '').trim();
    if (!name || !path) return;
    const csv = sel => (div.querySelector(sel).value || '').split(',').map(s => s.trim()).filter(Boolean);
    const path_params = csv('.wz-ep-pathp');
    const query_params = csv('.wz-ep-queryp');
    const body_params = csv('.wz-ep-bodyp');
    eps.push({name, method: div.querySelector('.wz-ep-method').value, path, path_params, query_params, body_params});
    const props = {}; const required = [];
    [...path_params, ...query_params, ...body_params].forEach(pn => { props[pn] = {type:'string'}; });
    path_params.forEach(pn => required.push(pn));
    tools[name] = {name, description: name, input_schema: {type:'object', properties: props, required}};
  });
  return {eps, tools};
}

// Wizard requirement/setup-command list helpers
function _wzListAdd(containerId, placeholder) {
  const c = document.getElementById(containerId);
  const idx = c.children.length;
  const div = document.createElement('div');
  div.className = 'list-row mt-1';
  div.innerHTML = `<input class="form-control form-control-sm font-monospace" placeholder="${placeholder}">
    <button class="btn-icon" onclick="this.parentElement.remove()" title="Remove">✕</button>`;
  c.appendChild(div);
}
function wzAddReq()          { _wzListAdd('wz-pkg-reqs-container',  'requests==2.32.0'); }
function wzAddSetupCmd()     { _wzListAdd('wz-pkg-cmds-container',  'npx playwright install chrome'); }
function wzAddCodeReq()      { _wzListAdd('wz-code-reqs-container', 'httpx'); }
function wzAddCodeSetupCmd() { _wzListAdd('wz-code-cmds-container', 'python -m playwright install chromium'); }

function _wzGetListValues(containerId) {
  return Array.from(document.querySelectorAll(`#${containerId} input`))
    .map(el => el.value.trim()).filter(Boolean);
}

async function wzNext() {
  const errEl = document.getElementById('wz-error');
  errEl.textContent = '';

  if (wzStep === 'type') return;

  if (wzStep === 'remote') {
    const name = document.getElementById('wz-remote-name').value.trim();
    const url  = document.getElementById('wz-remote-url').value.trim();
    if (!name) { errEl.textContent = 'Provider name is required.'; return; }
    if (!/^https?:\/\//i.test(url)) { errEl.textContent = 'A server URL starting with http:// or https:// is required.'; return; }
    // Bridge the remote server through mcp-remote, exactly like the Asana
    // example. The OAuth/token flow is handled by the shared introspect helper.
    const cmd = 'npx -y mcp-remote ' + url;
    const nextBtn = document.getElementById('wz-next-btn');
    nextBtn.disabled = true;
    const origText = nextBtn.textContent;
    nextBtn.textContent = '⏳ Introspecting…';
    try {
      await _wzIntrospectCommand(cmd, {resultEl: document.getElementById('wz-remote-result')});
    } finally {
      nextBtn.disabled = false;
      nextBtn.textContent = origText;
    }
    const provider = {
      name, type: 'package', command: cmd,
      documentation: 'Remote MCP server bridged via `mcp-remote` (' + url + '). ' +
        'Authentication (OAuth/token) is handled by mcp-remote and refreshed automatically.',
      code: '', requirements: [], setup_commands: [],
      tools: wzIntrospectedTools.map(t => ({
        name: t.name,
        function: '',
        description: t.description || '',
        documentation: '',
        enabled: true,
        parameters: _schemaToParams(t.inputSchema || t.input_schema || {}),
        secrets: [],
      })),
    };
    try {
      const r = await api('POST', '/api/tools', {name, provider});
      currentName = name; currentProvider = provider;
      loadList();
      await wzGoSecrets(r.secret_keys || []);
    } catch(e) { errEl.textContent = e.message; }
    return;
  }

  if (wzStep === 'package') {
    const name = document.getElementById('wz-pkg-name').value.trim();
    const cmd  = document.getElementById('wz-pkg-cmd').value.trim();
    if (!name) { errEl.textContent = 'Provider name is required.'; return; }
    if (!cmd)  { errEl.textContent = 'Command is required.'; return; }
    // Auto-introspect now (silent fallback on failure — the user can still
    // proceed and add tools by hand in the editor).
    const nextBtn = document.getElementById('wz-next-btn');
    nextBtn.disabled = true;
    const origText = nextBtn.textContent;
    nextBtn.textContent = '⏳ Introspecting…';
    try {
      await wzIntrospect();
    } finally {
      nextBtn.disabled = false;
      nextBtn.textContent = origText;
    }
    const requirements   = _wzGetListValues('wz-pkg-reqs-container');
    const setup_commands = _wzGetListValues('wz-pkg-cmds-container');
    const provider = {
      name, type: 'package', command: cmd, documentation: '', code: '',
      requirements, setup_commands,
      tools: wzIntrospectedTools.map(t => ({
        name: t.name,
        function: '',
        description: t.description || '',
        documentation: '',
        enabled: true,
        parameters: _schemaToParams(t.inputSchema || t.input_schema || {}),
        secrets: [],
      })),
    };
    try {
      const r = await api('POST', '/api/tools', {name, provider});
      currentName = name; currentProvider = provider;
      loadList();
      await wzGoSecrets(r.secret_keys || []);
    } catch(e) { errEl.textContent = e.message; }
    return;
  }

  if (wzStep === 'repository') {
    const name = document.getElementById('wz-repo-name').value.trim();
    const url  = document.getElementById('wz-repo-url').value.trim();
    const ref  = document.getElementById('wz-repo-ref').value.trim();
    const cmd  = document.getElementById('wz-repo-cmd').value.trim();
    if (!name) { errEl.textContent = 'Provider name is required.'; return; }
    if (!url)  { errEl.textContent = 'Git repository URL is required.'; return; }
    if (!cmd)  { errEl.textContent = 'Spawn command is required.'; return; }
    const build_commands = _wzGetListValues('wz-repo-builds-container');
    const nextBtn = document.getElementById('wz-next-btn');
    const origText = nextBtn.textContent;
    const resultEl = document.getElementById('wz-repo-result');
    nextBtn.disabled = true;

    let result;
    try {
      nextBtn.textContent = '⏳ Cloning & building…';
      resultEl.innerHTML = '<span class="text-muted" style="font-size:.875em">Cloning repo and running build commands — this may take a while…</span>';
      result = await _wzRepoBuildAndIntrospect({name, url, ref, build_commands, cmd, nextBtn});
    } catch (e) {
      errEl.textContent = e.message;
      resultEl.innerHTML = `<div class="text-danger" style="font-size:.875em">✗ ${esc(e.message)}</div>`;
      nextBtn.disabled = false; nextBtn.textContent = origText;
      return;
    } finally {
      nextBtn.disabled = false; nextBtn.textContent = origText;
    }

    // Render the outcome summary
    const lines = [];
    if (result.ok) {
      lines.push(`<div style="color:var(--green);font-size:.875em">✓ Built in <code>${esc(result.workdir)}</code></div>`);
    } else {
      lines.push(`<div class="text-warning" style="font-size:.875em">⚠ Build failed: ${esc(result.buildErr || '')}${result.failed_command ? ` (running <code>${esc(result.failed_command)}</code>)` : ''}.</div>`);
    }
    if (result.env_keys.length) {
      lines.push(`<div style="font-size:.875em;color:var(--yellow)">Discovered ${result.env_keys.length} env key(s) from .env.example — fill them in next.</div>`);
    }
    if (result.tools.length) {
      lines.push(`<div style="color:var(--green);font-size:.875em">✓ Found ${result.tools.length} tool(s)</div>`);
    } else if (result.introErr) {
      lines.push(`<div class="text-warning" style="font-size:.875em">⚠ Introspection failed (${esc(result.introErr)}).</div>`);
    }
    resultEl.innerHTML = lines.join('');

    // Stash state — finalisation happens after the Secrets step (or
    // immediately if no env_keys were discovered).
    wzRepoCtx = {
      name, command: cmd, repo_url: url, repo_ref: ref,
      build_commands, ...result,
    };

    if (result.env_keys.length) {
      // Defer provider creation until secrets are saved so we can re-run
      // the build with .env in place.
      await wzGoSecrets(result.env_keys);
    } else {
      await _wzRepoFinalize();
    }
    return;
  }

  if (wzStep === 'rest') {
    const name = document.getElementById('wz-rest-name').value.trim();
    const baseUrl = document.getElementById('wz-rest-base-url').value.trim();
    if (!name) { errEl.textContent = 'Provider name is required.'; return; }
    if (!baseUrl) { errEl.textContent = 'Base URL is required.'; return; }
    const openapi = document.getElementById('wz-rest-openapi').value.trim();
    const nextBtn = document.getElementById('wz-next-btn');
    const manualActive = document.getElementById('wz-rest-manual-pane').style.display !== 'none';
    if (manualActive) {
      const m = wzRestCollectManualEndpoints();
      wzRestEndpoints = m.eps; wzRestEndpointTools = m.tools;
    } else if (openapi && !wzRestEndpoints.length) {
      // OpenAPI source given but not yet introspected — do it now.
      nextBtn.disabled = true; const t = nextBtn.textContent; nextBtn.textContent = '⏳ Introspecting…';
      try { await wzRestIntrospect(); }
      finally { nextBtn.disabled = false; nextBtn.textContent = t; }
    }
    if (!wzRestEndpoints.length) {
      errEl.textContent = 'Add at least one endpoint, or import an OpenAPI spec.'; return;
    }
    const auth = wzRestCollectAuth();
    const authErr = wzRestValidateAuth(auth);
    if (authErr) { errEl.textContent = authErr; return; }
    const tools = wzRestEndpoints.map(ep => {
      const t = wzRestEndpointTools[ep.name];
      return {
        name: ep.name, function: '',
        description: (t && t.description) || ep.name,
        documentation: '', enabled: true,
        parameters: _schemaToParams((t && (t.input_schema || t.inputSchema)) || {}),
        secrets: [],
      };
    });
    const provider = {
      name, type: 'rest', command: '', code: '', documentation: '',
      requirements: ['httpx'], setup_commands: [],
      rest: { base_url: baseUrl, headers: {}, auth, openapi: '', endpoints: wzRestEndpoints },
      tools,
    };
    try {
      const r = await api('POST', '/api/tools', {name, provider});
      currentName = name; currentProvider = provider;
      loadList();
      await wzGoSecrets(r.secret_keys || []);
    } catch(e) { errEl.textContent = e.message; }
    return;
  }

  if (wzStep === 'code') {
    const name = document.getElementById('wz-code-name').value.trim();
    const code = document.getElementById('wz-code-input').value;
    if (!name) { errEl.textContent = 'Provider name is required.'; return; }
    if (!code.trim()) { errEl.textContent = 'Code is required.'; return; }
    const requirements   = _wzGetListValues('wz-code-reqs-container');
    const setup_commands = _wzGetListValues('wz-code-cmds-container');
    const fns = await _analyzeFns(code);
    const provider = {
      name, type: 'code', command: '', documentation: '', code,
      requirements, setup_commands,
      tools: fns.map(fn => ({
        name: fn.name, function: fn.name,
        description: `TODO — describe what ${fn.name} does`,
        documentation: '',
        enabled: true,
        parameters: fn.params.map(p => ({name:p.name,type:p.type||'string',description:'',required:true,default:null})),
        secrets: [],
      })),
    };
    try {
      const r = await api('POST', '/api/tools', {name, provider});
      currentName = name; currentProvider = provider;
      loadList();
      await wzGoSecrets(r.secret_keys || []);
    } catch(e) { errEl.textContent = e.message; }
    return;
  }

  if (wzStep === 'secrets') {
    await wzSaveSecretsAndFinish();
  }
}

function wzBack() {
  const map = {remote:'type', package:'type', repository:'type', rest:'type', code:'type', secrets: wzType||'type'};
  wzShowStep(map[wzStep] || 'type');
}

async function wzIntrospect() {
  const cmd  = document.getElementById('wz-pkg-cmd').value.trim();
  const el   = document.getElementById('wz-introspect-result');
  if (!cmd) {
    wzIntrospectedTools = [];
    el.innerHTML = '';
    return;
  }
  const requirements   = _wzGetListValues('wz-pkg-reqs-container');
  const setup_commands = _wzGetListValues('wz-pkg-cmds-container');
  await _wzIntrospectCommand(cmd, {requirements, setup_commands, resultEl: el});
}

// Shared introspection routine used by both the Package and Remote steps.
// Polls /api/pending-auth so OAuth-protected servers (bridged via mcp-remote)
// surface a clickable "Authorize" link while the handshake is blocked, then
// runs /api/introspect and stores the result in wzIntrospectedTools.
async function _wzIntrospectCommand(cmd, {requirements = [], setup_commands = [], resultEl} = {}) {
  const el = resultEl;
  el.innerHTML = '<span class="text-muted" style="font-size:.875em">Introspecting — this may take a moment on first use…</span>';
  // Remote OAuth servers (bridged via mcp-remote) block introspection until the
  // user authorizes in a browser.  Poll for the authorization URL meanwhile and
  // surface it as a clickable link so the wizard can walk the user through it.
  const authPoll = setInterval(async () => {
    try {
      const a = await api('GET', '/api/pending-auth?command=' + encodeURIComponent(cmd));
      if (a && a.auth_url) {
        el.innerHTML = `<div class="text-warning" style="font-size:.875em">🔐 Authorization required — `
          + `<a href="${esc(a.auth_url)}" target="_blank" rel="noopener">click here to authorize</a>, `
          + `then complete the browser flow. Introspection continues automatically once you finish.</div>`;
      }
    } catch {}
  }, 1500);
  try {
    const r = await api('POST', '/api/introspect', {command: cmd, requirements, setup_commands});
    if (!r.ok) throw new Error(r.error || 'Introspection failed');
    wzIntrospectedTools = r.tools || [];
    if (wzIntrospectedTools.length) {
      el.innerHTML = `<div style="color:var(--green);font-size:.875em">✓ Found ${wzIntrospectedTools.length} tool(s): <b>${esc(wzIntrospectedTools.map(t=>t.name).join(', '))}</b></div>`;
    } else {
      el.innerHTML = `<div class="text-muted" style="font-size:.875em">No tools advertised by this command — you can still add tools manually in the editor.</div>`;
    }
  } catch(e) {
    wzIntrospectedTools = [];
    el.innerHTML = `<div class="text-warning" style="font-size:.875em">⚠ Introspection failed (${esc(e.message)}). Continuing without auto-detected tools — add them manually in the editor.</div>`;
  } finally {
    clearInterval(authPoll);
  }
}

async function wzAnalyze() {
  const code = document.getElementById('wz-code-input').value;
  const el   = document.getElementById('wz-analyze-result');
  if (!code.trim()) { el.innerHTML = ''; return; }
  try {
    const r = await api('POST', '/api/extract-functions', {code});
    if (!r.ok) { el.innerHTML = `<span class="text-warning">⚠ ${esc(r.error)}</span>`; return; }
    if (!r.functions.length) { el.innerHTML = '<span class="text-muted">No <code>async def fn(context, …)</code> found yet.</span>'; return; }
    el.innerHTML = `<span style="color:var(--green)">✓ Found: <b>${esc(r.functions.map(f=>f.name).join(', '))}</b></span>`;
  } catch(e) { el.innerHTML = `<span class="text-warning">⚠ ${esc(e.message)}</span>`; }
}

async function _analyzeFns(code) {
  try {
    const r = await api('POST', '/api/extract-functions', {code});
    return r.ok ? r.functions : [];
  } catch { return []; }
}

async function wzGoSecrets(secretKeys) {
  const env = await api('GET', '/api/env').catch(() => ({vars:{}, env_file:'.env'}));
  document.getElementById('wz-env-path').textContent = env.env_file || '.env';
  const el = document.getElementById('wz-secrets-fields');
  if (!secretKeys.length) {
    el.innerHTML = '';
    document.getElementById('wz-secrets-none').style.display = '';
  } else {
    document.getElementById('wz-secrets-none').style.display = 'none';
    const existing = env.vars || {};
    el.innerHTML = secretKeys.map(k => {
      const isSet = !!existing[k];
      return `<div class="section-box ${isSet?'secret-set':'secret-unset'} mb-2">
        <div class="d-flex align-items-center gap-2 mb-1">
          <span class="fw-semibold font-monospace" style="font-size:.9em">${k}</span>
          ${isSet ? '<span style="color:var(--green);font-size:.8em">✓ set</span>' : ''}
        </div>
        <input class="form-control form-control-sm" type="password" id="secret-${k}"
          placeholder="${isSet ? 'leave blank to keep existing' : 'enter value…'}">
      </div>`;
    }).join('');
  }
  wzShowStep('secrets');
}

async function wzSaveSecretsAndFinish() {
  const inputs = document.querySelectorAll('#wz-secrets-fields input[type=password]');
  const vars = {};
  inputs.forEach(el => {
    const key = el.id.replace('secret-', '');
    if (el.value.trim()) vars[key] = el.value.trim();
  });
  if (Object.keys(vars).length) {
    try { await api('POST', '/api/env', {vars}); toast(`Saved ${Object.keys(vars).length} secret(s) ✓`); }
    catch(e) { toast(e.message, false); }
  }

  // Repository providers: with the secrets now in .env, retry the build
  // (which writes <workdir>/.env from os.environ) and re-introspect, then
  // finalise.  If the retry still fails we save anyway so the user can
  // edit manually — much better than getting stuck on the wizard.
  if (wzRepoCtx) {
    const nextBtn = document.getElementById('wz-next-btn');
    const origText = nextBtn.textContent;
    nextBtn.disabled = true;
    try {
      nextBtn.textContent = '⏳ Re-building with secrets…';
      const retry = await _wzRepoBuildAndIntrospect({
        name: wzRepoCtx.name,
        url: wzRepoCtx.repo_url,
        ref: wzRepoCtx.repo_ref,
        build_commands: wzRepoCtx.build_commands,
        cmd: wzRepoCtx.command,
        nextBtn,
      });
      Object.assign(wzRepoCtx, retry);
      if (!retry.ok) {
        toast(`Build still failing: ${retry.buildErr || ''}. Saving the provider anyway — use "↻ Re-clone & build" in the editor once you've fixed it.`, false);
      } else if (retry.introErr) {
        toast(`Build succeeded but introspection failed: ${retry.introErr}. Add tools manually in the editor.`, false);
      } else if (retry.tools.length) {
        toast(`Re-built ✓ · ${retry.tools.length} tool(s) introspected`);
      }
    } catch (e) {
      toast(`Build retry failed: ${e.message}`, false);
    } finally {
      nextBtn.disabled = false; nextBtn.textContent = origText;
    }
    await _wzRepoFinalize();
    return;
  }

  wizModal.hide();
  await loadList();
  await openProvider(currentName);
}

// ── Repository wizard helpers ─────────────────────────────────────────────

async function _wzRepoBuildAndIntrospect({name, url, ref, build_commands, cmd, nextBtn}) {
  const cb = await api('POST', '/api/clone-and-build', {
    name, repo_url: url, ref, build_commands,
  });
  const out = {
    ok: !!cb.ok,
    buildErr: cb.error || null,
    failed_command: cb.failed_command || null,
    workdir: cb.workdir || '',
    env_keys: cb.env_keys || [],
    tools: [],
    introErr: null,
  };
  if (cb.ok && cmd) {
    if (nextBtn) nextBtn.textContent = '⏳ Introspecting…';
    try {
      const ir = await api('POST', '/api/introspect', {
        command: cmd, cwd: out.workdir, env_keys: out.env_keys,
      });
      if (ir.ok) out.tools = ir.tools || [];
      else out.introErr = ir.error || 'introspection failed';
    } catch (e) {
      out.introErr = e.message;
    }
  }
  return out;
}

// Build the provider object and PUT it (idempotent across retries).
async function _wzRepoFinalize() {
  const ctx = wzRepoCtx;
  if (!ctx) return;
  let tools;
  if (ctx.tools && ctx.tools.length) {
    tools = ctx.tools.map(t => ({
      name: t.name,
      function: '',
      description: t.description || '',
      documentation: '',
      enabled: true,
      parameters: _schemaToParams(t.inputSchema || t.input_schema || {}),
      secrets: [],
    }));
  } else {
    // No tools yet (build still failing or introspection failed).  Insert a
    // disabled placeholder so create/update validation passes; user can
    // replace it from the editor after fixing the build.
    tools = [{
      name: '_placeholder', function: '',
      description: 'Placeholder — re-introspect once the build succeeds.',
      documentation: '', enabled: false, parameters: [], secrets: [],
    }];
  }
  const provider = {
    name: ctx.name, type: 'repository',
    command: ctx.command, documentation: '', code: '',
    repo_url: ctx.repo_url, repo_ref: ctx.repo_ref, workdir: ctx.workdir,
    build_commands: ctx.build_commands,
    repo_env_keys: ctx.env_keys,
    requirements: [], setup_commands: [],
    tools,
  };
  try {
    // PUT is idempotent — creates if missing, replaces if present.  This
    // makes the wizard safe to retry without 409 collisions.
    await api('PUT', `/api/tools/${ctx.name}`, {provider});
    currentName = ctx.name; currentProvider = provider;
  } catch (e) {
    toast(e.message, false);
  }
  wzRepoCtx = null;
  wizModal.hide();
  await loadList();
  if (currentName) await openProvider(currentName);
}

// ─────────────────────────────────────────────────────────────────────────────
// File manager (/api/files — browse / mkdir / upload / download / delete)
// ─────────────────────────────────────────────────────────────────────────────
async function openFiles() {
  filesModal.show();
  await filesRefresh();
}

function filesSetRoot(root) {
  filesRoot = root;
  filesPath = '';
  filesRefresh();
}

function filesNavigate(path) {
  filesPath = path;
  filesRefresh();
}

function _fmtSize(n) {
  if (n >= 1048576) return (n / 1048576).toFixed(1) + ' MB';
  if (n >= 1024) return (n / 1024).toFixed(1) + ' KB';
  return n + ' B';
}

async function filesRefresh() {
  let data;
  try {
    data = await api('GET', `/api/files?root=${encodeURIComponent(filesRoot)}&path=${encodeURIComponent(filesPath)}`);
  } catch (e) { toast(e.message, false); return; }
  filesRoots = data.roots;

  const sel = document.getElementById('files-root');
  sel.innerHTML = filesRoots.map(r =>
    `<option value="${esc(r)}" ${r === filesRoot ? 'selected' : ''}>${esc(r)}</option>`).join('');

  // Breadcrumb: root + each path segment is clickable.
  const segs = filesPath ? filesPath.split('/') : [];
  let crumbs = `<li class="breadcrumb-item"><a href="#" data-nav="">${esc(filesRoot)}</a></li>`;
  let acc = '';
  for (const s of segs) {
    acc = acc ? acc + '/' + s : s;
    crumbs += `<li class="breadcrumb-item"><a href="#" data-nav="${esc(acc)}">${esc(s)}</a></li>`;
  }
  const crumbsEl = document.getElementById('files-crumbs');
  crumbsEl.innerHTML = crumbs;
  crumbsEl.querySelectorAll('a[data-nav]').forEach(a => a.addEventListener('click', ev => {
    ev.preventDefault();
    filesNavigate(a.dataset.nav);
  }));

  const list = document.getElementById('files-list');
  if (!data.entries.length) {
    list.innerHTML = '<div class="empty-state">Empty directory.</div>';
    return;
  }
  list.innerHTML = data.entries.map(e => {
    const icon = e.type === 'directory' ? '📁' : e.type === 'symlink' ? '🔗' : '📄';
    const size = e.type === 'file' ? `<span class="text-muted me-3" style="font-size:.8em">${_fmtSize(e.size)}</span>` : '';
    return `<div class="provider-item" data-path="${esc(e.path)}" data-type="${e.type}">
      <span style="overflow:hidden;text-overflow:ellipsis">${icon} ${esc(e.name)}</span>
      <span class="d-flex align-items-center">${size}
        <button class="btn-icon" data-del="${esc(e.path)}" title="Delete">🗑</button>
      </span>
    </div>`;
  }).join('');
}

// Single delegated click handler — avoids quoting issues with arbitrary filenames.
function filesListClick(ev) {
  const delBtn = ev.target.closest('[data-del]');
  if (delBtn) {
    const row = delBtn.closest('.provider-item');
    filesDelete(delBtn.dataset.del, row.dataset.type === 'directory');
    return;
  }
  const row = ev.target.closest('.provider-item');
  if (!row) return;
  if (row.dataset.type === 'directory') {
    filesNavigate(row.dataset.path);
  } else if (row.dataset.type === 'file') {
    window.open(`/api/files/download?root=${encodeURIComponent(filesRoot)}&path=${encodeURIComponent(row.dataset.path)}`, '_blank');
  }
}

async function filesMkdir() {
  const name = prompt('New folder name (e.g. secrets):');
  if (!name) return;
  const path = filesPath ? `${filesPath}/${name}` : name;
  try {
    await api('POST', '/api/files/mkdir', {root: filesRoot, path});
    toast(`Created ${path}`);
    await filesRefresh();
  } catch (e) { toast(e.message, false); }
}

async function filesUpload(fileList) {
  // Multipart upload — must NOT go through api() (it forces a JSON content type).
  for (const file of fileList) {
    const form = new FormData();
    form.append('root', filesRoot);
    form.append('path', filesPath);
    form.append('file', file);
    try {
      const r = await fetch('/api/files/upload', {method: 'POST', body: form});
      const data = await r.json().catch(() => ({detail: r.statusText}));
      if (!r.ok) throw new Error(data.detail || r.statusText);
      toast(`Uploaded ${data.path}`);
    } catch (e) { toast(`${file.name}: ${e.message}`, false); }
  }
  document.getElementById('files-upload-input').value = '';
  await filesRefresh();
}

async function filesDelete(path, isDir) {
  if (!confirm(`Delete ${path}?`)) return;
  let recursive = false;
  try {
    await api('DELETE', `/api/files?root=${encodeURIComponent(filesRoot)}&path=${encodeURIComponent(path)}`);
  } catch (e) {
    if (isDir && e.message.includes('not empty')) {
      if (!confirm(`${path} is not empty — delete it and everything inside?`)) return;
      recursive = true;
    } else { toast(e.message, false); return; }
  }
  if (recursive) {
    try {
      await api('DELETE', `/api/files?root=${encodeURIComponent(filesRoot)}&path=${encodeURIComponent(path)}&recursive=true`);
    } catch (e) { toast(e.message, false); return; }
  }
  toast(`Deleted ${path}`);
  await filesRefresh();
}

// ─────────────────────────────────────────────────────────────────────────────
// Tool tester (/v1/tools list + /v1/tools/{name}/invoke)
// ─────────────────────────────────────────────────────────────────────────────
async function openToolTester() {
  ttModal.show();
  try {
    const r = await api('GET', '/v1/tools');
    ttTools = r.tools || [];
  } catch (e) { toast(e.message, false); ttTools = []; }
  ttRenderList();
  if (ttSelected && !ttTools.some(t => t.function.name === ttSelected)) {
    ttSelected = null;
    document.getElementById('tt-detail').innerHTML =
      '<div class="empty-state" style="margin-top:60px">Select a tool to test it.</div>';
  }
}

function ttRenderList() {
  const q = (document.getElementById('tt-search').value || '').toLowerCase();
  const listEl = document.getElementById('tt-list');
  const tools = ttTools.filter(t => t.function.name.toLowerCase().includes(q));
  if (!ttTools.length) {
    listEl.innerHTML = `<div class="empty-state">No tools registered.<br><br>
      The registry is populated at server startup — after provider changes,
      restart and reopen this dialog.<br><br>
      <button class="btn btn-sm btn-outline-light" onclick="restartServer()">⟳ Restart server</button>
    </div>`;
    return;
  }
  if (!tools.length) {
    listEl.innerHTML = '<div class="empty-state">No tools match the filter.</div>';
    return;
  }
  // Group by provider prefix (advertised names look like provider__tool).
  const groups = {};
  for (const t of tools) {
    const name = t.function.name;
    const prov = name.includes('__') ? name.split('__')[0] : '(other)';
    (groups[prov] = groups[prov] || []).push(t);
  }
  let out = '';
  for (const prov of Object.keys(groups).sort()) {
    out += `<div class="section-title" style="padding:8px 12px 0;margin-bottom:2px">${esc(prov)}</div>`;
    out += groups[prov].map(t => {
      const name = t.function.name;
      const short = name.includes('__') ? name.split('__').slice(1).join('__') : name;
      return `<div class="provider-item ${name === ttSelected ? 'active' : ''}" data-tool="${esc(name)}">
        <span style="overflow:hidden;text-overflow:ellipsis" title="${esc(t.function.description)}">${esc(short)}</span>
      </div>`;
    }).join('');
  }
  listEl.innerHTML = out;
}

function ttListClick(ev) {
  const row = ev.target.closest('[data-tool]');
  if (!row) return;
  ttSelected = row.dataset.tool;
  ttRenderList();
  ttRenderForm();
}

function ttRenderForm() {
  const tool = ttTools.find(t => t.function.name === ttSelected);
  if (!tool) return;
  const schema = tool.function.parameters || {};
  const props = schema.properties || {};
  const required = new Set(schema.required || []);
  let fields = '';
  for (const [name, def] of Object.entries(props)) {
    const isReq = required.has(name);
    const badge = `<span class="req-badge ${isReq ? 'req-yes' : 'req-no'}">${isReq ? 'required' : 'optional'}</span>`;
    const help = def.description
      ? `<div class="text-muted" style="font-size:.78em;margin-top:2px">${esc(def.description)}</div>` : '';
    const type = def.type || (def.enum ? 'string' : 'json');
    let input;
    if (def.enum) {
      const blank = isReq ? '' : '<option value=""></option>';
      input = `<select class="form-select form-select-sm tt-field" data-name="${esc(name)}" data-kind="enum">${blank}` +
        def.enum.map(v => `<option ${String(def.default) === String(v) ? 'selected' : ''}>${esc(v)}</option>`).join('') +
        '</select>';
    } else if (type === 'boolean') {
      input = `<div class="form-check">
        <input class="form-check-input tt-field" type="checkbox" data-name="${esc(name)}" data-kind="boolean"
          ${def.default === true ? 'checked' : ''} id="tt-f-${esc(name)}">
        <label class="form-check-label" for="tt-f-${esc(name)}" style="font-size:.85em">enabled</label>
      </div>`;
    } else if (type === 'number' || type === 'integer') {
      input = `<input class="form-control form-control-sm tt-field" type="number" data-name="${esc(name)}"
        data-kind="${type}" ${type === 'integer' ? 'step="1"' : 'step="any"'}
        value="${def.default != null ? esc(def.default) : ''}">`;
    } else if (type === 'string') {
      input = `<input class="form-control form-control-sm tt-field" data-name="${esc(name)}" data-kind="string"
        value="${def.default != null ? esc(def.default) : ''}">`;
    } else {  // object / array / unknown — raw JSON
      const placeholder = JSON.stringify(def.default ?? (type === 'array' ? [] : {}), null, 2);
      input = `<textarea class="form-control form-control-sm font-monospace tt-field" rows="3"
        data-name="${esc(name)}" data-kind="json" spellcheck="false">${esc(placeholder)}</textarea>`;
    }
    fields += `<div class="mb-2">
      <label class="form-label mb-1">${esc(name)} ${badge}
        <span class="text-muted" style="text-transform:none;letter-spacing:0">(${esc(type)})</span></label>
      ${input}${help}
    </div>`;
  }
  if (!fields) fields = '<div class="text-muted mb-2" style="font-size:.85em">This tool takes no arguments.</div>';
  document.getElementById('tt-detail').innerHTML = `
    <h6 style="word-break:break-all">${esc(tool.function.name)}</h6>
    <div class="text-muted mb-3" style="font-size:.85em;white-space:pre-wrap">${esc(tool.function.description)}</div>
    <div class="section-box"><div class="section-title">Arguments</div><div id="tt-form">${fields}</div></div>
    <button class="btn btn-sm btn-success" id="tt-invoke-btn" onclick="ttInvoke()">▶ Invoke</button>
    <div id="tt-result" class="mt-3"></div>`;
}

function ttCollectArgs() {
  const tool = ttTools.find(t => t.function.name === ttSelected);
  const required = new Set((tool.function.parameters || {}).required || []);
  const args = {};
  for (const el of document.querySelectorAll('#tt-form .tt-field')) {
    const name = el.dataset.name, kind = el.dataset.kind;
    if (kind === 'boolean') { args[name] = el.checked; continue; }
    const raw = el.value;
    if (raw === '') {  // empty optional fields are omitted so handler defaults apply
      if (required.has(name)) throw new Error(`'${name}' is required`);
      continue;
    }
    if (kind === 'integer') args[name] = parseInt(raw, 10);
    else if (kind === 'number') args[name] = Number(raw);
    else if (kind === 'json') {
      try { args[name] = JSON.parse(raw); }
      catch { throw new Error(`'${name}' is not valid JSON`); }
    } else args[name] = raw;
  }
  return args;
}

async function ttInvoke() {
  let args;
  try { args = ttCollectArgs(); }
  catch (e) { toast(e.message, false); return; }
  const btn = document.getElementById('tt-invoke-btn');
  const resultEl = document.getElementById('tt-result');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Running…';
  resultEl.innerHTML = '';
  try {
    const res = await api('POST', `/v1/tools/${encodeURIComponent(ttSelected)}/invoke`, {arguments: args});
    ttRenderResult(res);
  } catch (e) {
    ttRenderResult({content: [{type: 'text', text: e.message}], is_error: true});
  } finally {
    btn.disabled = false;
    btn.innerHTML = '▶ Invoke';
  }
}

function ttRenderResult(res) {
  const el = document.getElementById('tt-result');
  const border = res.is_error ? 'border-left:3px solid var(--red)' : 'border-left:3px solid var(--green)';
  const badge = res.is_error ? '<span class="badge-err">✗ error</span>'
                             : '<span class="badge-repo" style="font-size:.62em">✓ ok</span>';
  el.innerHTML = `<div class="section-box" style="${border}">
    <div class="section-title">Result ${badge}</div>
    <div id="tt-result-body"></div></div>`;
  const body = document.getElementById('tt-result-body');
  for (const item of (res.content || [])) {
    const pre = document.createElement('pre');
    pre.style.cssText = 'white-space:pre-wrap;word-break:break-word;font-size:.8em;margin:0 0 8px;color:#cdd6f4;max-height:50vh;overflow:auto';
    if (item.type === 'text') {
      let text = item.text;
      try { text = JSON.stringify(JSON.parse(item.text), null, 2); } catch {}
      pre.textContent = text;  // textContent — tool output is untrusted
    } else {
      pre.textContent = JSON.stringify(item, null, 2);
    }
    body.appendChild(pre);
  }
  if (!(res.content || []).length) body.innerHTML = '<span class="text-muted" style="font-size:.85em">(empty result)</span>';
}

// ─────────────────────────────────────────────────────────────────────────────
// Helpers
// ─────────────────────────────────────────────────────────────────────────────
function esc(str) {
  return String(str||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function _schemaToParams(schema) {
  const props = (schema.properties || schema.inputSchema?.properties) || {};
  const required = new Set((schema.required || schema.inputSchema?.required) || []);
  return Object.entries(props).map(([name, def]) => ({
    name,
    type: def.type || 'string',
    description: def.description || '',
    required: required.has(name),
    default: def.default ?? null,
  }));
}
</script>
</body>
</html>"""

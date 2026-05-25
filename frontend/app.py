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
POST /api/restart               — send SIGTERM to restart server
"""

import ast
import asyncio
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import textwrap
import threading
import traceback
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from config import CONFIG_DIR, ENV_FILE, REPOS_DIR


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
    return keys


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
    if repo_sub is not None:
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
        "tools": tools_out,
    }


def _structured_to_yaml(provider: dict[str, Any]) -> str:
    """Convert the structured JSON provider dict back to a YAML string."""
    spec: dict[str, Any] = {}

    doc = (provider.get("documentation") or "").strip()
    if doc:
        spec["documentation"] = doc + "\n"

    ptype = provider.get("type", "code")

    if ptype == "package":
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

def _validate_provider(provider: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    ptype = provider.get("type", "code")

    if ptype == "package":
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

def create_app(config_dir: Path | None = None, env_file: Path | None = None) -> "FastAPI":
    _config_dir = config_dir or CONFIG_DIR
    _env_file = env_file or ENV_FILE

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
                out.append({
                    "name": path.stem,
                    "file": path.name,
                    "tool_count": len(tool_entries),
                    "tool_names": [t.get("name") for t in tool_entries],
                    "provider_type": structured["type"],
                    "is_package": is_package,
                    "is_repository": is_repository,
                    "secret_keys": secret_keys,
                    "missing_secrets": missing_secrets,
                    "validation_errors": validation["errors"],
                    "documentation": spec.get("documentation") or "",
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
  </div>
  <span class="ms-auto text-muted" style="font-size:.75em">MCP :8888 &nbsp;|&nbsp; UI :8889</span>
</nav>

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
          <div class="section-title">📦 Package Command</div>
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
            <div class="col-md-4">
              <div class="card wizard-choice h-100" onclick="wzSelectType('code')">
                <div class="card-body text-center p-4">
                  <div style="font-size:2.5em">🐍</div>
                  <h6 class="mt-2">Python Code</h6>
                  <small class="text-muted">Write <code>async def</code> functions — each one becomes an MCP tool</small>
                </div>
              </div>
            </div>
            <div class="col-md-4">
              <div class="card wizard-choice h-100" onclick="wzSelectType('package')">
                <div class="card-body text-center p-4">
                  <div style="font-size:2.5em">📦</div>
                  <h6 class="mt-2">Package</h6>
                  <small class="text-muted">Run an existing MCP server via <code>npx</code>, <code>uvx</code>, <code>python -m</code>, or any command — tools are auto-detected</small>
                </div>
              </div>
            </div>
            <div class="col-md-4">
              <div class="card wizard-choice h-100" onclick="wzSelectType('repository')">
                <div class="card-body text-center p-4">
                  <div style="font-size:2.5em">📂</div>
                  <h6 class="mt-2">Repository</h6>
                  <small class="text-muted">Clone a git repo, run build commands, then introspect & spawn the resulting stdio MCP server</small>
                </div>
              </div>
            </div>
          </div>
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
            <div class="text-muted mt-1" style="font-size:.8em">Any command that spawns a stdio MCP server (npx, uvx, python -m, or an installed binary).</div>
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
            <label class="form-label">Build commands <span class="text-muted fw-normal" style="text-transform:none">run inside the cloned workdir, in order — must terminate</span></label>
            <div id="wz-repo-builds-container"></div>
            <button class="btn btn-sm btn-outline-secondary py-0 mt-1" onclick="wzAddRepoBuild()">+ Add command</button>
            <div class="text-muted mt-1" style="font-size:.8em">e.g. <code>npm install</code>, <code>npm run build</code>. <b>Do not</b> put the long-running server start here (e.g. <code>npm run start:dev</code>) — that goes in <b>Spawn command</b>. Build commands re-run on every server start so ephemeral containers rebuild.</div>
          </div>
          <div class="mb-3">
            <label class="form-label">Spawn command *</label>
            <input class="form-control font-monospace" id="wz-repo-cmd"
              placeholder="node dist/main.js">
            <div class="text-muted mt-1" style="font-size:.8em">The long-running command that launches the stdio MCP server, run from inside the workdir after the build commands complete.</div>
          </div>
          <div class="text-muted" style="font-size:.8em">Clicking <b>Next</b> clones the repo, parses <code>.env.example</code> (so its keys appear as secrets on the next step), runs the build commands, then introspects the spawn command to populate the tool list. If the build fails because secrets aren't set yet, you can still continue — the next server restart will re-build with the secrets in place.</div>
          <div id="wz-repo-result" class="mt-2"></div>
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

<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/codemirror.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/mode/python/python.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/js/bootstrap.bundle.min.js"></script>
<script>
// ─────────────────────────────────────────────────────────────────────────────
// State
// ─────────────────────────────────────────────────────────────────────────────
let currentName = null;
let currentProvider = null;   // the structured JSON object being edited
let codeEditor = null;        // CodeMirror instance for the code block
let secretsModal = null, wizModal = null;
let wzType = null;            // 'code' | 'package' | 'repository'
let wzStep = 'type';
let wzIntrospectedTools = []; // tools returned by introspect
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
  // Wizard: live function detection as the user types into the code textarea
  document.getElementById('wz-code-input').addEventListener('input', () => {
    clearTimeout(_wzAnalyzeDebounce);
    _wzAnalyzeDebounce = setTimeout(() => wzAnalyze().catch(() => {}), 300);
  });
  loadList();
});

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
      const alertRow = (warnBadge || errBadge)
        ? `<div class="d-flex gap-1 flex-wrap mt-1">${warnBadge}${errBadge}</div>`
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
  const isPkg = currentProvider.type === 'package' || currentProvider.type === 'repository';
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
  const isPkg = p.type === 'package' || isRepo; // repo also uses package.command
  const isCode = p.type === 'code';
  const label = isRepo ? ' (repository)' : isPkg ? ' (package)' : ' (code)';
  document.getElementById('editor-title').textContent = p.name + label;
  document.getElementById('f-documentation').value = p.documentation || '';

  document.getElementById('package-box').style.display = isPkg ? '' : 'none';
  document.getElementById('repository-box').style.display = isRepo ? '' : 'none';
  document.getElementById('code-box').style.display = isCode ? '' : 'none';

  if (isPkg) {
    document.getElementById('f-command').value = p.command || '';
  }
  if (isRepo) {
    document.getElementById('f-repo-url').value = p.repo_url || '';
    document.getElementById('f-repo-ref').value = p.repo_ref || '';
    document.getElementById('f-repo-workdir').textContent = p.workdir || '(auto)';
    renderBuildCommands(p.build_commands || []);
    renderEnvKeys(p.repo_env_keys || []);
  }
  if (isCode) {
    codeEditor.setValue(p.code || '');
    setTimeout(() => codeEditor.refresh(), 50);
  }

  renderRequirements(p.requirements || []);
  renderSetupCommands(p.setup_commands || []);
  renderTools(p.tools || [], isPkg);
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
  const isPkg = currentProvider.type === 'package' || currentProvider.type === 'repository';
  currentProvider.tools.push({
    name: '', function: '', description: '', documentation: '',
    enabled: true, parameters: [], secrets: [],
  });
  renderTools(currentProvider.tools, isPkg);
}

function removeTool(i) {
  ensureProvider();
  currentProvider.tools.splice(i, 1);
  renderTools(currentProvider.tools, currentProvider.type === 'package' || currentProvider.type === 'repository');
}

function addParam(ti) {
  ensureProvider();
  currentProvider.tools[ti].parameters.push({name:'',type:'string',description:'',required:false,default:null});
  renderTools(currentProvider.tools, currentProvider.type === 'package' || currentProvider.type === 'repository');
}

function removeParam(ti, pi) {
  ensureProvider();
  currentProvider.tools[ti].parameters.splice(pi, 1);
  renderTools(currentProvider.tools, currentProvider.type === 'package' || currentProvider.type === 'repository');
}

function addSecret(ti) {
  ensureProvider();
  currentProvider.tools[ti].secrets.push({arg:'',env:''});
  renderTools(currentProvider.tools, currentProvider.type === 'package' || currentProvider.type === 'repository');
}

function removeSecret(ti, si) {
  ensureProvider();
  currentProvider.tools[ti].secrets.splice(si, 1);
  renderTools(currentProvider.tools, currentProvider.type === 'package' || currentProvider.type === 'repository');
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
const WZ_STEPS = ['type','package','repository','code','secrets'];

function wzShowStep(step) {
  WZ_STEPS.forEach(s => {
    const el = document.getElementById(`wz-${s}`);
    if (el) el.classList.toggle('active', s === step);
  });
  wzStep = step;
  document.getElementById('wz-back-btn').style.display = step === 'type' ? 'none' : '';
  document.getElementById('wz-next-btn').textContent = step === 'secrets' ? 'Finish' : 'Next →';
  document.getElementById('wz-error').textContent = '';
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
  wzShowStep('type');
  wizModal.show();
}

function wzAddRepoBuild() { _wzListAdd('wz-repo-builds-container', 'npm install'); }

function wzSelectType(type) {
  wzType = type;
  document.querySelectorAll('.wizard-choice').forEach(el => el.classList.remove('selected'));
  event.currentTarget.classList.add('selected');
  setTimeout(() => wzShowStep(type), 120);
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
  const map = {package:'type', repository:'type', code:'type', secrets: wzType||'type'};
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
  el.innerHTML = '<span class="text-muted" style="font-size:.875em">Introspecting — this may take a moment on first use…</span>';
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

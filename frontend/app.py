"""
HTTP frontend for mcpproxy — served on port 8889 (MCP stays on 8888).

Endpoints
---------
GET  /                        — single-page HTML UI
GET  /api/tools               — list providers
GET  /api/tools/{name}        — get raw YAML content
POST /api/tools               — create provider  {name, content}
PUT  /api/tools/{name}        — overwrite YAML   {content}
DELETE /api/tools/{name}      — delete YAML
POST /api/validate            — validate YAML structure {content}
POST /api/generate-skeleton   — generate YAML template {source, ...}
POST /api/extract-functions   — parse Python code       {code}
GET  /api/known-servers       — curated importable repos
GET  /api/env                 — list .env vars (values masked)
POST /api/env                 — upsert vars into .env  {vars: {KEY: VALUE}}
POST /api/restart             — send SIGTERM to MCP process
"""

import ast
import io
import os
import re
import signal
import textwrap
import threading
import traceback
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from config import CONFIG_DIR, ENV_FILE

# ---------------------------------------------------------------------------
# Curated MCP server list shown in the Discover tab
# ---------------------------------------------------------------------------

KNOWN_SERVERS: list[dict[str, Any]] = [
    {
        "name": "Filesystem",
        "description": "Read/write local files and directories. Official Anthropic reference server.",
        "repo_url": "https://github.com/modelcontextprotocol/servers",
        "branch": "main",
        "subfolder": "src/filesystem",
        "link": "https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem",
        "tags": ["official", "files"],
    },
    {
        "name": "GitHub",
        "description": "Search repos, create issues/PRs, manage files via GitHub API.",
        "repo_url": "https://github.com/modelcontextprotocol/servers",
        "branch": "main",
        "subfolder": "src/github",
        "link": "https://github.com/modelcontextprotocol/servers/tree/main/src/github",
        "tags": ["official", "github"],
    },
    {
        "name": "Brave Search",
        "description": "Web and local search powered by Brave Search API.",
        "repo_url": "https://github.com/modelcontextprotocol/servers",
        "branch": "main",
        "subfolder": "src/brave-search",
        "link": "https://github.com/modelcontextprotocol/servers/tree/main/src/brave-search",
        "tags": ["official", "search"],
    },
    {
        "name": "Fetch",
        "description": "Fetch URLs and convert web content to Markdown.",
        "repo_url": "https://github.com/modelcontextprotocol/servers",
        "branch": "main",
        "subfolder": "src/fetch",
        "link": "https://github.com/modelcontextprotocol/servers/tree/main/src/fetch",
        "tags": ["official", "web"],
    },
    {
        "name": "SQLite",
        "description": "Read and write a local SQLite database.",
        "repo_url": "https://github.com/modelcontextprotocol/servers",
        "branch": "main",
        "subfolder": "src/sqlite",
        "link": "https://github.com/modelcontextprotocol/servers/tree/main/src/sqlite",
        "tags": ["official", "database"],
    },
    {
        "name": "Postgres",
        "description": "Query and inspect a PostgreSQL database.",
        "repo_url": "https://github.com/modelcontextprotocol/servers",
        "branch": "main",
        "subfolder": "src/postgres",
        "link": "https://github.com/modelcontextprotocol/servers/tree/main/src/postgres",
        "tags": ["official", "database"],
    },
    {
        "name": "awesome-mcp-servers (browse)",
        "description": "Community curated list of hundreds of MCP servers across all categories.",
        "repo_url": None,
        "link": "https://github.com/punkpeye/awesome-mcp-servers",
        "tags": ["directory"],
    },
    {
        "name": "mcpservers.org (browse)",
        "description": "Searchable directory of MCP servers with categories and ratings.",
        "repo_url": None,
        "link": "https://mcpservers.org",
        "tags": ["directory"],
    },
]

# ---------------------------------------------------------------------------
# YAML skeleton templates
# ---------------------------------------------------------------------------

BLANK_TEMPLATE = textwrap.dedent("""\
    # Provider name appears in the filename: <name>.yaml
    # Add more tools by adding entries under 'tools:'.

    code: |
      from typing import Any

      async def my_function(context: dict[str, Any], message: str) -> dict[str, Any]:
          \"\"\"Tool implementation — replace this with real logic.\"\"\"
          return {
              "ok": True,
              "echo": message,
          }

    tools:
      - name: my_tool
        function: my_function
        description: Echo a message back (replace with a real description).
        input_schema:
          type: object
          properties:
            message:
              type: string
              description: The text to echo.
          required:
            - message
""")

REPO_TEMPLATE = textwrap.dedent("""\
    # This provider clones an external repo at startup and imports from it.
    # Edit the 'code:' block to call functions from the repo.

    repo:
      url: {url}
      branch: {branch}{subfolder_line}{requirements_line}{packages_block}

    code: |
      # The repo root{subfolder_note} is on sys.path — import directly.
      from typing import Any

      # Example: from your_module import some_function

      async def example_tool(context: dict[str, Any]) -> dict[str, Any]:
          \"\"\"Replace with a real tool that calls into the imported repo.\"\"\"
          return {{"ok": True, "message": "Hello from {repo_name}"}}

    tools:
      - name: {provider_name}_example
        function: example_tool
        description: TODO — replace with a description of what this tool does.
        input_schema:
          type: object
          properties: {{}}
          required: []
""")

# ---------------------------------------------------------------------------
# .env helpers
# ---------------------------------------------------------------------------

def _read_env_file(path: Path) -> dict[str, str]:
    """Parse a .env file into {KEY: VALUE} (strips quotes, ignores comments)."""
    result: dict[str, str] = {}
    if not path.exists():
        return result
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        result[key] = val
    return result


def _write_env_file(path: Path, updates: dict[str, str]) -> None:
    """
    Upsert *updates* into *path*.  Existing lines are preserved; new keys are
    appended.  The file is created if it does not exist.
    """
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
            # Replace in-place
            for i, line in enumerate(new_lines):
                stripped = line.strip()
                if stripped and not stripped.startswith("#") and stripped.partition("=")[0].strip() == key:
                    new_lines[i] = new_line
                    break
        else:
            new_lines.append(new_line)

    path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


def _extract_secret_env_keys(spec: dict[str, Any]) -> list[str]:
    """Return all env var names declared in secrets.env across all tools."""
    keys: list[str] = []
    for tool in spec.get("tools", []):
        for key in (tool.get("secrets") or {}).get("env", {}).values():
            if key not in keys:
                keys.append(key)
    return keys


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

def create_app(config_dir: Path | None = None, env_file: Path | None = None) -> FastAPI:
    _config_dir = config_dir or CONFIG_DIR
    _env_file = env_file or ENV_FILE

    app = FastAPI(title="mcpproxy UI", docs_url=None, redoc_url=None)

    # ── Tool CRUD ────────────────────────────────────────────────────────────

    @app.get("/api/tools")
    async def list_tools() -> list[dict]:
        tools: list[dict] = []
        if not _config_dir.exists():
            return tools
        for path in sorted(_config_dir.glob("*.yaml")):
            try:
                spec = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                tool_entries = spec.get("tools") or []
                tools.append({
                    "name": path.stem,
                    "file": path.name,
                    "tool_count": len(tool_entries),
                    "tool_names": [t.get("name") for t in tool_entries],
                    "has_repo": "repo" in spec,
                    "secret_keys": _extract_secret_env_keys(spec),
                    "documentation": spec.get("documentation") or "",
                })
            except Exception as exc:
                tools.append({"name": path.stem, "file": path.name, "error": str(exc)})
        return tools

    @app.get("/api/tools/{name}")
    async def get_tool(name: str) -> dict:
        _guard_name(name)
        path = _config_dir / f"{name}.yaml"
        if not path.exists():
            raise HTTPException(404, f"Provider '{name}' not found")
        content = path.read_text(encoding="utf-8")
        spec = yaml.safe_load(content) or {}
        return {
            "name": name,
            "content": content,
            "secret_keys": _extract_secret_env_keys(spec),
            "documentation": spec.get("documentation") or "",
        }

    @app.post("/api/tools")
    async def create_tool(request: Request) -> dict:
        body = await request.json()
        name = (body.get("name") or "").strip()
        content = body.get("content") or ""
        _guard_name(name)
        path = _config_dir / f"{name}.yaml"
        if path.exists():
            raise HTTPException(409, f"Provider '{name}' already exists")
        _validate_or_raise(content)
        _config_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        spec = yaml.safe_load(content) or {}
        return {"ok": True, "name": name, "secret_keys": _extract_secret_env_keys(spec)}

    @app.put("/api/tools/{name}")
    async def update_tool(name: str, request: Request) -> dict:
        _guard_name(name)
        body = await request.json()
        content = body.get("content") or ""
        _validate_or_raise(content)
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

    # ── YAML validation ──────────────────────────────────────────────────────

    @app.post("/api/validate")
    async def validate_yaml(request: Request) -> dict:
        body = await request.json()
        content = body.get("content") or ""
        return _validate_spec(content)

    # ── Skeleton generator ───────────────────────────────────────────────────

    @app.post("/api/generate-skeleton")
    async def generate_skeleton(request: Request) -> dict:
        body = await request.json()
        source = body.get("source", "blank")

        if source == "blank":
            return {"ok": True, "yaml": BLANK_TEMPLATE}

        if source == "code":
            code = body.get("code") or ""
            return {"ok": True, "yaml": _skeleton_from_code(code)}

        if source == "repo":
            return {"ok": True, "yaml": _skeleton_from_repo(body)}

        raise HTTPException(400, f"Unknown source '{source}'")

    # ── Function extractor ───────────────────────────────────────────────────

    @app.post("/api/extract-functions")
    async def extract_functions(request: Request) -> dict:
        body = await request.json()
        code = body.get("code") or ""
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
                    for a in node.args.args
                    if a.arg != "context"
                ]
                functions.append({"name": node.name, "params": params})
        return {"ok": True, "functions": functions}

    # ── Known servers ─────────────────────────────────────────────────────────

    @app.get("/api/known-servers")
    async def known_servers() -> list:
        return KNOWN_SERVERS

    # ── .env management ──────────────────────────────────────────────────────

    @app.get("/api/env")
    async def get_env() -> dict:
        current = _read_env_file(_env_file)
        # Return keys + masked values so the UI can show which are set
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
        # Reject obviously dangerous key names
        for k in updates:
            if not re.match(r'^[A-Z][A-Z0-9_]*$', k):
                raise HTTPException(400, f"Invalid env var name: {k!r}")
        _write_env_file(_env_file, updates)
        return {"ok": True, "written": list(updates.keys())}

    # ── Restart ───────────────────────────────────────────────────────────────

    @app.post("/api/restart")
    async def restart() -> dict:
        def _send_signal():
            import time
            time.sleep(0.4)
            try:
                os.kill(os.getpid(), signal.SIGTERM)
            except Exception:
                pass
        threading.Thread(target=_send_signal, daemon=True).start()
        return {"ok": True}

    # ── HTML UI ───────────────────────────────────────────────────────────────

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return _HTML

    return app


# ---------------------------------------------------------------------------
# Input helpers
# ---------------------------------------------------------------------------

def _guard_name(name: str) -> None:
    if not name:
        raise HTTPException(400, "name is required")
    if not re.match(r'^[a-zA-Z][a-zA-Z0-9_\-]*$', name):
        raise HTTPException(400, "name must start with a letter and contain only letters, digits, _ or -")


def _validate_or_raise(content: str) -> dict:
    result = _validate_spec(content)
    if not result["ok"]:
        raise HTTPException(400, "; ".join(result["errors"]))
    return result


def _validate_spec(content: str) -> dict:
    try:
        spec = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        return {"ok": False, "errors": [f"YAML parse error: {exc}"]}
    errors: list[str] = []
    if not isinstance(spec, dict):
        return {"ok": False, "errors": ["Root must be a YAML mapping"]}
    has_code = bool(spec.get("code", "").strip() if isinstance(spec.get("code"), str) else spec.get("code"))
    has_repo = bool(spec.get("repo"))
    if not has_code and not has_repo:
        errors.append("Missing 'code' block (or 'repo' for external-repo providers)")
    tools = spec.get("tools")
    if not tools:
        errors.append("Missing 'tools' list")
    else:
        for i, t in enumerate(tools):
            if not isinstance(t, dict):
                errors.append(f"tools[{i}]: must be a mapping")
                continue
            for field in ("name", "function", "description", "input_schema"):
                if field not in t:
                    errors.append(f"tools[{i}]: missing '{field}'")
    return {"ok": len(errors) == 0, "errors": errors}


def _skeleton_from_code(code: str) -> str:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return BLANK_TEMPLATE

    tools_yaml_parts: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        arg_names = [a.arg for a in node.args.args]
        if "context" not in arg_names:
            continue
        params = [a.arg for a in node.args.args if a.arg != "context"]
        props = "\n".join(
            f"        {p}:\n          type: string\n          description: TODO" for p in params
        )
        required = "[" + ", ".join(params) + "]" if params else "[]"
        tools_yaml_parts.append(
            f"  - name: {node.name}\n"
            f"    function: {node.name}\n"
            f"    description: TODO — describe what {node.name} does\n"
            f"    input_schema:\n"
            f"      type: object\n"
            f"      properties:\n{props if props else '        {}'}\n"
            f"      required: {required}"
        )

    if not tools_yaml_parts:
        return BLANK_TEMPLATE

    tools_block = "\n\n".join(tools_yaml_parts)
    return f"code: |\n" + textwrap.indent(code, "  ") + "\n\ntools:\n" + tools_block + "\n"


def _skeleton_from_repo(body: dict[str, Any]) -> str:
    url = body.get("repo_url") or ""
    branch = body.get("branch") or "main"
    subfolder = (body.get("subfolder") or "").strip()
    requirements = (body.get("requirements") or "requirements.txt").strip()
    packages: list[str] = [p for p in (body.get("packages") or []) if p]

    # Derive a short name from the URL for placeholder text
    repo_name = url.rstrip("/").split("/")[-1] if url else "repo"
    provider_name = re.sub(r"[^a-zA-Z0-9_]", "_", repo_name.removesuffix(".git"))

    subfolder_line = f"\n  subfolder: {subfolder}" if subfolder else ""
    requirements_line = f"\n  requirements: {requirements}" if requirements != "requirements.txt" else ""
    packages_block = ""
    if packages:
        pkg_lines = "\n".join(f"    - {p}" for p in packages)
        packages_block = f"\n  install_packages:\n{pkg_lines}"
    subfolder_note = f"/{subfolder}" if subfolder else ""

    return REPO_TEMPLATE.format(
        url=url,
        branch=branch,
        subfolder_line=subfolder_line,
        requirements_line=requirements_line,
        packages_block=packages_block,
        repo_name=repo_name,
        provider_name=provider_name,
        subfolder_note=subfolder_note,
    )


# ---------------------------------------------------------------------------
# HTML template (single-page app)
# ---------------------------------------------------------------------------

_HTML = """<!DOCTYPE html>
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
body{background:var(--bg);color:#cdd6f4;min-height:100vh}
.navbar{background:var(--surface)!important;border-bottom:1px solid var(--border)}
.navbar-brand{font-weight:700;letter-spacing:.5px}
.card{background:var(--surface);border:1px solid var(--border);color:#cdd6f4}
.card-header{background:var(--border);border-bottom:1px solid #45475a;font-weight:600;padding:.5rem .75rem}
.tool-item{cursor:pointer;padding:8px 12px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:flex-start;transition:background .15s}
.tool-item:hover{background:#252535}
.tool-item.active{background:#2a2a3e;border-left:2px solid var(--accent)}
.badge-repo{background:#74c7ec;color:#1e1e2e;font-size:.65em;padding:2px 5px;border-radius:3px}
.badge-count{background:#45475a;color:#cdd6f4;font-size:.65em;padding:2px 5px;border-radius:3px}
.CodeMirror{height:calc(100vh - 220px);font-size:13px;border-radius:0 0 6px 6px;font-family:'JetBrains Mono',Consolas,monospace}
#wizard-yaml-editor .CodeMirror{height:340px}
.v-ok{border-left:3px solid var(--green);background:#1a2a1b;padding:8px 12px;border-radius:4px;font-size:.875em}
.v-err{border-left:3px solid var(--red);background:#2a1b1b;padding:8px 12px;border-radius:4px;font-size:.875em}
.modal-content{background:var(--bg);border:1px solid var(--border);color:#cdd6f4}
.modal-header,.modal-footer{border-color:var(--border)}
.btn-close-white{filter:invert(1)}
.form-control,.form-select{background:#313244;border:1px solid #45475a;color:#cdd6f4}
.form-control:focus,.form-select:focus{background:#313244;border-color:var(--accent);color:#cdd6f4;box-shadow:0 0 0 .2rem rgba(137,180,250,.2)}
.form-control::placeholder{color:#6c7086}
.form-label{color:var(--muted);font-size:.85em;margin-bottom:.25rem}
.wizard-choice{cursor:pointer;transition:border-color .15s,background .15s}
.wizard-choice:hover{border-color:var(--accent)!important;background:#252535}
.wizard-step{display:none}
.wizard-step.active{display:block}
.server-card{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:14px;margin-bottom:10px}
.nav-tabs{border-bottom-color:var(--border)}
.nav-tabs .nav-link{color:var(--muted);border:none;border-bottom:2px solid transparent;padding:.4rem .75rem}
.nav-tabs .nav-link.active{color:#cdd6f4;border-bottom-color:var(--accent);background:transparent}
.secret-row{background:#252535;border:1px solid var(--border);border-radius:6px;padding:10px;margin-bottom:8px}
.secret-set{border-left:3px solid var(--green)}
.secret-unset{border-left:3px solid var(--yellow)}
a{color:var(--accent)}
code{color:var(--teal);background:#252535;padding:1px 4px;border-radius:3px;font-size:.85em}
</style>
</head>
<body>

<!-- Navbar -->
<nav class="navbar navbar-dark navbar-expand">
  <div class="container-fluid gap-3">
    <span class="navbar-brand">⚡ mcpproxy</span>
    <div class="d-flex gap-2">
      <button class="btn btn-sm btn-outline-secondary" onclick="showView('tools')" id="nav-tools">🔧 Tools</button>
      <button class="btn btn-sm btn-outline-secondary" onclick="showView('discover')" id="nav-discover">🔍 Discover</button>
      <button class="btn btn-sm btn-success" onclick="openWizard()">+ New Provider</button>
    </div>
    <span class="ms-auto text-muted" style="font-size:.75em">MCP → :8888 &nbsp; UI → :8889</span>
  </div>
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

<!-- ═══════════════════════════ TOOLS VIEW ════════════════════════════════ -->
<div id="view-tools" class="container-fluid mt-3">
  <div class="row g-3">
    <div class="col-md-3">
      <div class="card">
        <div class="card-header d-flex justify-content-between align-items-center">
          Providers
          <button class="btn btn-sm btn-outline-secondary py-0 px-2" onclick="loadToolList()" title="Refresh">↻</button>
        </div>
        <div id="tool-list"><div class="p-3 text-muted">Loading…</div></div>
      </div>
    </div>
    <div class="col-md-9">
      <div class="card">
        <div class="card-header d-flex justify-content-between align-items-center">
          <span id="editor-title" class="text-muted">Select a provider to edit</span>
          <div class="d-flex gap-2" id="editor-actions" style="display:none!important">
            <button class="btn btn-sm btn-outline-warning" onclick="validateCurrent()">Validate</button>
            <button class="btn btn-sm btn-outline-info" onclick="openSecretsModal()">🔑 Secrets</button>
            <button class="btn btn-sm btn-primary" onclick="saveCurrent()">Save</button>
            <button class="btn btn-sm btn-outline-danger" onclick="deleteCurrent()">Delete</button>
          </div>
        </div>
        <div id="validation-result" style="display:none" class="mx-3 mt-2"></div>
        <textarea id="yaml-editor"></textarea>
      </div>
      <div id="restart-note" style="display:none" class="mt-2 text-muted" style="font-size:.8em">
        ⚠️ Changes take effect after restarting the MCP server.
        <button class="btn btn-sm btn-outline-danger ms-2" onclick="restartServer()">Restart MCP Server</button>
      </div>
      <div id="docs-panel" style="display:none" class="card mt-3">
        <div class="card-header d-flex justify-content-between align-items-center">
          <span>📖 Provider Documentation</span>
          <button class="btn btn-sm btn-outline-secondary py-0 px-2" onclick="toggleDocs()" id="docs-toggle">Hide</button>
        </div>
        <div class="card-body" id="docs-content" style="white-space:pre-wrap;font-size:.875em;max-height:400px;overflow-y:auto"></div>
      </div>
    </div>
  </div>
</div>

<!-- ═══════════════════════════ DISCOVER VIEW ════════════════════════════ -->
<div id="view-discover" class="container-fluid mt-3" style="display:none">
  <div class="row g-3">
    <div class="col-md-8">
      <h6 class="text-muted mb-3">CURATED SERVERS</h6>
      <div id="known-servers-list"><div class="text-muted">Loading…</div></div>
    </div>
    <div class="col-md-4">
      <div class="card">
        <div class="card-header">External Directories</div>
        <div class="card-body">
          <ul class="list-unstyled mb-0">
            <li class="mb-3"><a href="https://github.com/punkpeye/awesome-mcp-servers" target="_blank">📋 punkpeye/awesome-mcp-servers</a><br><small class="text-muted">Community curated mega-list</small></li>
            <li class="mb-3"><a href="https://mcpservers.org" target="_blank">🌐 mcpservers.org</a><br><small class="text-muted">Searchable server directory</small></li>
            <li class="mb-3"><a href="https://github.com/modelcontextprotocol/servers" target="_blank">📦 modelcontextprotocol/servers</a><br><small class="text-muted">Official reference implementations</small></li>
            <li><a href="https://github.com/wong2/awesome-mcp-servers" target="_blank">⭐ wong2/awesome-mcp-servers</a><br><small class="text-muted">Another curated list</small></li>
          </ul>
        </div>
      </div>
    </div>
  </div>
</div>

<!-- ═══════════════════════════ SECRETS MODAL ════════════════════════════ -->
<div class="modal fade" id="secrets-modal" tabindex="-1">
  <div class="modal-dialog modal-lg">
    <div class="modal-content">
      <div class="modal-header">
        <h5 class="modal-title">🔑 Manage Secrets — <span id="secrets-provider-name"></span></h5>
        <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button>
      </div>
      <div class="modal-body" id="secrets-modal-body">
        <p class="text-muted">Values are written to <code id="secrets-env-path">.env</code>. The file is never committed to git.</p>
        <div id="secrets-fields"></div>
      </div>
      <div class="modal-footer">
        <button class="btn btn-outline-secondary" data-bs-dismiss="modal">Cancel</button>
        <button class="btn btn-primary" onclick="saveSecrets()">Save to .env</button>
      </div>
    </div>
  </div>
</div>

<!-- ═══════════════════════════ WIZARD MODAL ═════════════════════════════ -->
<div class="modal fade" id="wizard-modal" tabindex="-1">
  <div class="modal-dialog modal-xl modal-dialog-scrollable">
    <div class="modal-content">
      <div class="modal-header">
        <h5 class="modal-title">New MCP Provider</h5>
        <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button>
      </div>
      <div class="modal-body">

        <!-- Step 1: source -->
        <div id="wz-source" class="wizard-step active">
          <p class="text-muted mb-4">How do you want to create this provider?</p>
          <div class="row g-3">
            <div class="col-md-4">
              <div class="card wizard-choice h-100" onclick="wzSelectSource('blank')">
                <div class="card-body text-center p-4">
                  <div style="font-size:2.5em">📝</div>
                  <h6 class="mt-2">Blank Template</h6>
                  <small class="text-muted">Start from a documented template and write your own code</small>
                </div>
              </div>
            </div>
            <div class="col-md-4">
              <div class="card wizard-choice h-100" onclick="wzSelectSource('code')">
                <div class="card-body text-center p-4">
                  <div style="font-size:2.5em">🐍</div>
                  <h6 class="mt-2">From Python Code</h6>
                  <small class="text-muted">Paste async functions — YAML skeleton is generated automatically</small>
                </div>
              </div>
            </div>
            <div class="col-md-4">
              <div class="card wizard-choice h-100" onclick="wzSelectSource('repo')">
                <div class="card-body text-center p-4">
                  <div style="font-size:2.5em">📦</div>
                  <h6 class="mt-2">From Git Repo</h6>
                  <small class="text-muted">Point to a GitHub repo — server clones it at startup and injects it into sys.path</small>
                </div>
              </div>
            </div>
          </div>
        </div>

        <!-- Step 2a: paste code -->
        <div id="wz-code" class="wizard-step">
          <p class="text-muted">Paste Python <code>async def</code> functions. Each function with a <code>context</code> first argument becomes an MCP tool.</p>
          <div class="mb-2">
            <label class="form-label">Python Code</label>
            <textarea id="wz-code-input" class="form-control font-monospace" rows="12" placeholder="async def my_tool(context, query: str) -> dict:&#10;    return {'ok': True, 'result': query}"></textarea>
          </div>
          <button class="btn btn-sm btn-outline-info" onclick="wzAnalyze()">🔍 Analyze Functions</button>
          <div id="wz-analyze-result" class="mt-2"></div>
        </div>

        <!-- Step 2b: repo -->
        <div id="wz-repo" class="wizard-step">
          <p class="text-muted">The server will clone this repo at startup and prepend it to <code>sys.path</code>.</p>
          <div class="row g-3">
            <div class="col-md-8">
              <label class="form-label">Repository URL *</label>
              <input class="form-control" id="wz-repo-url" placeholder="https://github.com/user/some-mcp-server">
            </div>
            <div class="col-md-4">
              <label class="form-label">Branch</label>
              <input class="form-control" id="wz-repo-branch" value="main">
            </div>
            <div class="col-md-6">
              <label class="form-label">Subfolder added to sys.path <span class="text-muted">(optional)</span></label>
              <input class="form-control" id="wz-repo-subfolder" placeholder="src">
            </div>
            <div class="col-md-6">
              <label class="form-label">Requirements file <span class="text-muted">(leave blank for requirements.txt)</span></label>
              <input class="form-control" id="wz-repo-req" placeholder="requirements.txt">
            </div>
            <div class="col-12">
              <label class="form-label">Additional pip packages <span class="text-muted">(comma-separated)</span></label>
              <input class="form-control" id="wz-repo-pkgs" placeholder="requests, some-library">
            </div>
          </div>
        </div>

        <!-- Step 3: editor + name -->
        <div id="wz-editor" class="wizard-step">
          <div class="row g-2 mb-3">
            <div class="col-md-4">
              <label class="form-label">Provider name <span class="text-muted">(filename without .yaml)</span></label>
              <input class="form-control" id="wz-name" placeholder="my_provider">
            </div>
            <div class="col-md-8 d-flex align-items-end">
              <div id="wz-editor-validation" style="font-size:.85em;width:100%"></div>
            </div>
          </div>
          <textarea id="wizard-yaml-editor"></textarea>
        </div>

        <!-- Step 4: secrets -->
        <div id="wz-secrets" class="wizard-step">
          <p class="text-muted">The following environment variables are required by this provider. Enter their values to save them to <code id="wz-env-path">.env</code>.</p>
          <div id="wz-secrets-fields"></div>
          <div id="wz-secrets-none" style="display:none" class="text-muted">No secrets declared in this provider.</div>
        </div>

      </div>
      <div class="modal-footer">
        <button class="btn btn-outline-secondary" id="wz-back-btn" onclick="wzBack()" style="display:none">← Back</button>
        <button class="btn btn-outline-secondary" data-bs-dismiss="modal">Cancel</button>
        <button class="btn btn-primary" id="wz-next-btn" onclick="wzNext()">Next →</button>
      </div>
    </div>
  </div>
</div>

<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/codemirror.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/mode/yaml/yaml.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/mode/python/python.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/js/bootstrap.bundle.min.js"></script>
<script>
// ── State ──────────────────────────────────────────────────────────────────
let currentTool = null, isNew = false, currentSecretKeys = [];
let editor = null, wizEditor = null;
let wzModal = null, secretsModal = null;
let wzStep = 'source', wzSource = null;
const WZ_STEPS = ['source','code','repo','editor','secrets'];

// ── Boot ───────────────────────────────────────────────────────────────────
window.addEventListener('DOMContentLoaded', () => {
  editor = CM('yaml-editor', 'yaml', 'calc(100vh - 220px)');
  wizEditor = CM('wizard-yaml-editor', 'yaml', '340px');
  wzModal = new bootstrap.Modal('#wizard-modal');
  secretsModal = new bootstrap.Modal('#secrets-modal');
  loadToolList();
  loadKnownServers();
  showView('tools');
});

function CM(id, mode, height) {
  const ta = document.getElementById(id);
  const cm = CodeMirror.fromTextArea(ta, {
    mode, theme:'dracula', lineNumbers:true,
    indentWithTabs:false, indentUnit:2, tabSize:2,
  });
  cm.getWrapperElement().style.height = height;
  return cm;
}

// ── API ────────────────────────────────────────────────────────────────────
async function api(method, path, body) {
  const r = await fetch(path, {
    method,
    headers: {'Content-Type':'application/json'},
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const data = await r.json().catch(() => ({detail: r.statusText}));
  if (!r.ok) throw new Error(data.detail || r.statusText);
  return data;
}

// ── Toast ──────────────────────────────────────────────────────────────────
function toast(msg, ok=true) {
  const el = document.getElementById('toast');
  el.className = `toast text-white border-0 bg-${ok?'success':'danger'}`;
  document.getElementById('toast-msg').textContent = msg;
  bootstrap.Toast.getOrCreateInstance(el, {delay:3200}).show();
}

// ── Views ──────────────────────────────────────────────────────────────────
function showView(name) {
  document.getElementById('view-tools').style.display = name==='tools' ? '' : 'none';
  document.getElementById('view-discover').style.display = name==='discover' ? '' : 'none';
  document.getElementById('nav-tools').classList.toggle('btn-outline-light', name==='tools');
  document.getElementById('nav-tools').classList.toggle('btn-outline-secondary', name!=='tools');
  document.getElementById('nav-discover').classList.toggle('btn-outline-light', name==='discover');
  document.getElementById('nav-discover').classList.toggle('btn-outline-secondary', name!=='discover');
}

// ── Tool list ──────────────────────────────────────────────────────────────
async function loadToolList() {
  try {
    const tools = await api('GET', '/api/tools');
    const el = document.getElementById('tool-list');
    if (!tools.length) {
      el.innerHTML = '<div class="p-3 text-muted">No providers yet — click <b>+ New Provider</b>.</div>';
      return;
    }
    el.innerHTML = tools.map(t => `
      <div class="tool-item ${t.name===currentTool?'active':''}" onclick="openTool('${t.name}')">
        <div style="min-width:0">
          <div class="fw-semibold">${t.name}</div>
          <small class="text-muted d-block text-truncate">${(t.tool_names||[]).join(', ')||'no tools'}</small>
        </div>
        <div class="d-flex flex-column gap-1 align-items-end ms-1 flex-shrink-0">
          ${t.has_repo?'<span class="badge-repo">repo</span>':''}
          <span class="badge-count">${t.tool_count}</span>
        </div>
      </div>`).join('');
  } catch(e) {
    document.getElementById('tool-list').innerHTML = `<div class="p-3 text-danger">Error: ${e.message}</div>`;
  }
}

async function openTool(name) {
  try {
    const data = await api('GET', `/api/tools/${name}`);
    currentTool = name; isNew = false;
    currentSecretKeys = data.secret_keys || [];
    editor.setValue(data.content);
    setTimeout(() => editor.refresh(), 50);
    document.getElementById('editor-title').textContent = name + '.yaml';
    document.getElementById('editor-actions').style.display = 'flex';
    document.getElementById('restart-note').style.display = 'block';
    document.getElementById('validation-result').style.display = 'none';
    // Show documentation panel if present
    const docsPanel = document.getElementById('docs-panel');
    const docsContent = document.getElementById('docs-content');
    if (data.documentation) {
      docsContent.textContent = data.documentation;
      docsPanel.style.display = 'block';
      document.getElementById('docs-toggle').textContent = 'Hide';
    } else {
      docsPanel.style.display = 'none';
    }
    document.querySelectorAll('.tool-item').forEach(el => {
      el.classList.toggle('active', el.querySelector('.fw-semibold')?.textContent === name);
    });
  } catch(e) { toast(e.message, false); }
}

function toggleDocs() {
  const content = document.getElementById('docs-content');
  const btn = document.getElementById('docs-toggle');
  const hidden = content.style.display === 'none';
  content.style.display = hidden ? '' : 'none';
  btn.textContent = hidden ? 'Hide' : 'Show';
}

async function saveCurrent() {
  const content = editor.getValue();
  try {
    if (isNew) {
      await api('POST', '/api/tools', {name: currentTool, content});
      isNew = false;
    } else {
      const r = await api('PUT', `/api/tools/${currentTool}`, {content});
      currentSecretKeys = r.secret_keys || [];
    }
    toast('Saved ✓');
    loadToolList();
  } catch(e) { toast(e.message, false); }
}

async function deleteCurrent() {
  if (!confirm(`Delete ${currentTool}.yaml?`)) return;
  try {
    await api('DELETE', `/api/tools/${currentTool}`);
    currentTool = null; isNew = false; currentSecretKeys = [];
    editor.setValue('');
    document.getElementById('editor-title').textContent = 'Select a provider to edit';
    document.getElementById('editor-actions').style.display = 'none';
    document.getElementById('restart-note').style.display = 'none';
    document.getElementById('validation-result').style.display = 'none';
    toast('Deleted ✓');
    loadToolList();
  } catch(e) { toast(e.message, false); }
}

async function validateCurrent() {
  const el = document.getElementById('validation-result');
  try {
    const r = await api('POST', '/api/validate', {content: editor.getValue()});
    el.style.display = 'block';
    el.className = r.ok ? 'mx-3 mt-2 v-ok' : 'mx-3 mt-2 v-err';
    el.innerHTML = r.ok
      ? '✅ Valid — correct YAML structure'
      : '❌ Issues:<ul class="mb-0 mt-1 ps-3">' + r.errors.map(e=>`<li>${e}</li>`).join('') + '</ul>';
  } catch(e) { toast(e.message, false); }
}

async function restartServer() {
  try { await api('POST', '/api/restart'); toast('Restart signal sent — reconnect in a moment'); }
  catch(e) { toast('Signal sent', true); }
}

// ── Discover ───────────────────────────────────────────────────────────────
async function loadKnownServers() {
  try {
    const servers = await api('GET', '/api/known-servers');
    document.getElementById('known-servers-list').innerHTML = servers.map(s => `
      <div class="server-card">
        <div class="d-flex justify-content-between align-items-start">
          <div style="min-width:0">
            <div class="fw-semibold">${s.name}</div>
            <div class="text-muted" style="font-size:.875em">${s.description}</div>
            ${s.repo_url ? `<code style="font-size:.8em">${s.repo_url}</code>` : ''}
          </div>
          <div class="d-flex gap-2 ms-2 flex-shrink-0">
            ${s.link ? `<a href="${s.link}" target="_blank" class="btn btn-sm btn-outline-secondary">Browse</a>` : ''}
            ${s.repo_url ? `<button class="btn btn-sm btn-success" onclick="importRepo('${s.repo_url}','${s.branch||'main'}','${s.subfolder||''}')">Import</button>` : ''}
          </div>
        </div>
      </div>`).join('');
  } catch(e) {}
}

function importRepo(url, branch, subfolder) {
  openWizard('repo');
  document.getElementById('wz-repo-url').value = url;
  document.getElementById('wz-repo-branch').value = branch || 'main';
  document.getElementById('wz-repo-subfolder').value = subfolder || '';
}

// ── Secrets modal (edit view) ─────────────────────────────────────────────
async function openSecretsModal() {
  if (!currentTool) return;
  document.getElementById('secrets-provider-name').textContent = currentTool;
  const current = await api('GET', '/api/env').catch(() => ({vars:{}, env_file:'.env'}));
  document.getElementById('secrets-env-path').textContent = current.env_file || '.env';
  renderSecretFields('secrets-fields', currentSecretKeys, current.vars || {});
  secretsModal.show();
}

function renderSecretFields(containerId, keys, existing) {
  const el = document.getElementById(containerId);
  if (!keys.length) {
    el.innerHTML = '<div class="text-muted">No secrets declared.</div>';
    return;
  }
  el.innerHTML = keys.map(k => {
    const isSet = !!existing[k];
    return `<div class="secret-row ${isSet?'secret-set':'secret-unset'}">
      <div class="d-flex align-items-center gap-2 mb-1">
        <span class="fw-semibold font-monospace" style="font-size:.9em">${k}</span>
        ${isSet ? '<span class="text-success" style="font-size:.8em">✓ already set</span>' : '<span class="text-warning" style="font-size:.8em">not set</span>'}
      </div>
      <input class="form-control form-control-sm" type="password" id="secret-${k}"
        placeholder="${isSet ? '(leave blank to keep existing)' : 'Enter value…'}">
    </div>`;
  }).join('');
}

async function saveSecrets() {
  const keys = currentSecretKeys.length
    ? currentSecretKeys
    : [...document.querySelectorAll('[id^="secret-"]')].map(el => el.id.replace('secret-',''));
  const vars = {};
  for (const k of keys) {
    const el = document.getElementById(`secret-${k}`);
    if (el && el.value.trim()) vars[k] = el.value.trim();
  }
  if (!Object.keys(vars).length) { secretsModal.hide(); return; }
  try {
    await api('POST', '/api/env', {vars});
    toast(`Saved ${Object.keys(vars).length} secret(s) to .env ✓`);
    secretsModal.hide();
  } catch(e) { toast(e.message, false); }
}

// ── Wizard ─────────────────────────────────────────────────────────────────
function wzShowStep(step) {
  WZ_STEPS.forEach(s => {
    const el = document.getElementById(`wz-${s}`);
    if (el) el.classList.toggle('active', s === step);
  });
  wzStep = step;
  const backBtn = document.getElementById('wz-back-btn');
  const nextBtn = document.getElementById('wz-next-btn');
  backBtn.style.display = step === 'source' ? 'none' : '';
  nextBtn.textContent = step === 'secrets' ? 'Finish' : step === 'editor' ? 'Next → Secrets' : 'Next →';
}

function openWizard(preselect) {
  wzStep = 'source'; wzSource = preselect || null;
  WZ_STEPS.forEach(s => { const el = document.getElementById(`wz-${s}`); if(el) el.classList.remove('active'); });
  document.getElementById('wz-name').value = '';
  document.getElementById('wz-code-input').value = '';
  document.getElementById('wz-analyze-result').innerHTML = '';
  if (preselect) { wzSelectSource(preselect); }
  else { wzShowStep('source'); }
  wzModal.show();
}

function wzSelectSource(src) {
  wzSource = src;
  if (src === 'blank') { wzGenerateBlank(); }
  else { wzShowStep(src); }
}

async function wzAnalyze() {
  const code = document.getElementById('wz-code-input').value;
  try {
    const r = await api('POST', '/api/extract-functions', {code});
    const el = document.getElementById('wz-analyze-result');
    if (!r.ok) { el.innerHTML = `<div class="v-err mt-2">${r.error}</div>`; return; }
    if (!r.functions.length) { el.innerHTML = `<div class="v-err mt-2">No <code>async def fn(context, ...)</code> found.</div>`; return; }
    el.innerHTML = `<div class="v-ok mt-2">Found: <b>${r.functions.map(f=>f.name).join(', ')}</b></div>`;
  } catch(e) { toast(e.message, false); }
}

async function wzNext() {
  if (wzStep === 'source') return; // handled by card click
  if (wzStep === 'code') { await wzGenerateFromCode(); }
  else if (wzStep === 'repo') { await wzGenerateFromRepo(); }
  else if (wzStep === 'editor') { await wzSaveAndGoSecrets(); }
  else if (wzStep === 'secrets') { await wzSaveSecretsAndFinish(); }
}

function wzBack() {
  const map = {code:'source', repo:'source', editor: wzSource||'source', secrets:'editor'};
  wzShowStep(map[wzStep] || 'source');
}

async function wzGenerateBlank() {
  const r = await api('POST', '/api/generate-skeleton', {source:'blank'});
  wzShowEditorStep(r.yaml);
}

async function wzGenerateFromCode() {
  const code = document.getElementById('wz-code-input').value;
  const r = await api('POST', '/api/generate-skeleton', {source:'code', code});
  wzShowEditorStep(r.yaml);
}

async function wzGenerateFromRepo() {
  const url = document.getElementById('wz-repo-url').value.trim();
  if (!url) { toast('Repository URL is required', false); return; }
  const r = await api('POST', '/api/generate-skeleton', {
    source: 'repo',
    repo_url: url,
    branch: document.getElementById('wz-repo-branch').value || 'main',
    subfolder: document.getElementById('wz-repo-subfolder').value.trim(),
    requirements: document.getElementById('wz-repo-req').value.trim(),
    packages: (document.getElementById('wz-repo-pkgs').value||'').split(',').map(s=>s.trim()).filter(Boolean),
  });
  wzShowEditorStep(r.yaml);
}

function wzShowEditorStep(yaml) {
  wizEditor.setValue(yaml);
  setTimeout(() => wizEditor.refresh(), 100);
  document.getElementById('wz-editor-validation').innerHTML = '';
  wzShowStep('editor');
}

async function wzSaveAndGoSecrets() {
  const name = document.getElementById('wz-name').value.trim();
  const content = wizEditor.getValue();
  const el = document.getElementById('wz-editor-validation');

  if (!name) { el.innerHTML = '<span class="text-danger">Provider name is required.</span>'; return; }

  const v = await api('POST', '/api/validate', {content}).catch(e => ({ok:false, errors:[e.message]}));
  if (!v.ok) {
    el.innerHTML = '<span class="text-danger">❌ ' + v.errors.join(' · ') + '</span>';
    return;
  }
  el.innerHTML = '';

  try {
    const result = await api('POST', '/api/tools', {name, content});
    currentTool = name; isNew = false;
    currentSecretKeys = result.secret_keys || [];
    loadToolList();

    // Load secrets step
    const env = await api('GET', '/api/env').catch(() => ({vars:{}, env_file:'.env'}));
    document.getElementById('wz-env-path').textContent = env.env_file || '.env';
    renderSecretFields('wz-secrets-fields', currentSecretKeys, env.vars || {});
    document.getElementById('wz-secrets-none').style.display = currentSecretKeys.length ? 'none' : '';
    wzShowStep('secrets');
  } catch(e) { el.innerHTML = `<span class="text-danger">${e.message}</span>`; }
}

async function wzSaveSecretsAndFinish() {
  const keys = currentSecretKeys;
  const vars = {};
  for (const k of keys) {
    const el = document.getElementById(`secret-${k}`);
    if (el && el.value.trim()) vars[k] = el.value.trim();
  }
  if (Object.keys(vars).length) {
    try { await api('POST', '/api/env', {vars}); toast(`Saved ${Object.keys(vars).length} secret(s) ✓`); }
    catch(e) { toast(e.message, false); }
  }
  wzModal.hide();
  await openTool(currentTool);
}
</script>
</body>
</html>"""

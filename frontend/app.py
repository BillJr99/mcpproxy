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
POST /api/introspect-npx        — run npx command, return tools list
POST /api/extract-functions     — parse Python code for async functions
GET  /api/env                   — list .env vars (values masked)
POST /api/env                   — upsert vars into .env  {vars: {KEY: VALUE}}
POST /api/restart               — send SIGTERM to restart server
"""

import ast
import asyncio
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
    return keys


# ---------------------------------------------------------------------------
# Structured ↔ YAML conversion
# ---------------------------------------------------------------------------

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
            "parameters": params,
            "secrets": secrets,
        })

    return {
        "name": name,
        "documentation": spec.get("documentation", ""),
        "type": "npx" if spec.get("npx") else "code",
        "npx_command": (spec.get("npx") or {}).get("command", ""),
        "code": spec.get("code", ""),
        "tools": tools_out,
    }


def _structured_to_yaml(provider: dict[str, Any]) -> str:
    """Convert the structured JSON provider dict back to a YAML string."""
    spec: dict[str, Any] = {}

    doc = (provider.get("documentation") or "").strip()
    if doc:
        spec["documentation"] = doc + "\n"

    ptype = provider.get("type", "code")

    if ptype == "npx":
        spec["npx"] = {"command": (provider.get("npx_command") or "").strip()}
    else:
        code = (provider.get("code") or "").strip()
        if code:
            spec["code"] = code + "\n"

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

    if ptype == "npx":
        if not (provider.get("npx_command") or "").strip():
            errors.append("npx_command is required for npx providers")
    else:
        if not (provider.get("code") or "").strip():
            errors.append("code is required for code providers")

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
        for path in sorted(_config_dir.glob("*.yaml")):
            try:
                spec = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                tool_entries = spec.get("tools") or []
                out.append({
                    "name": path.stem,
                    "file": path.name,
                    "tool_count": len(tool_entries),
                    "tool_names": [t.get("name") for t in tool_entries],
                    "is_npx": bool(spec.get("npx")),
                    "secret_keys": _extract_secret_env_keys(spec),
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

    # ── npx introspection ────────────────────────────────────────────────────

    @app.post("/api/introspect-npx")
    async def introspect_npx(request: Request) -> dict:
        body = await request.json()
        command = (body.get("command") or "").strip()
        if not command:
            raise HTTPException(400, "command is required")
        try:
            from npx_runner import introspect
            tools = await introspect(command)
            return {"ok": True, "tools": tools}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "tools": []}

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
/* CodeMirror */
.CodeMirror{height:260px;font-size:13px;border-radius:0 0 6px 6px;font-family:'JetBrains Mono',Consolas,monospace;border:1px solid #45475a;border-top:none}
.cm-wrap{border:1px solid #45475a;border-radius:6px;overflow:hidden}
.cm-label{background:#313244;padding:4px 10px;font-size:.75em;color:var(--muted);border:1px solid #45475a;border-bottom:none;border-radius:6px 6px 0 0;font-weight:600;text-transform:uppercase;letter-spacing:.4px}
/* badges */
.badge-npx{background:#cba6f7;color:#1e1e2e;font-size:.65em;padding:2px 6px;border-radius:3px;font-weight:700}
.badge-code{background:#89b4fa;color:#1e1e2e;font-size:.65em;padding:2px 6px;border-radius:3px;font-weight:700}
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

        <!-- Documentation box -->
        <div class="section-box">
          <div class="section-title">📖 Documentation <span class="text-muted fw-normal" style="text-transform:none;letter-spacing:0;font-size:.9em">optional — shown in the UI, not sent to LLM</span></div>
          <textarea id="f-documentation" class="form-control" rows="3" placeholder="Describe what this provider does, its tools, any usage notes…" style="font-size:.875em;resize:vertical"></textarea>
        </div>

        <!-- npx command box (npx providers) -->
        <div class="section-box" id="npx-box" style="display:none">
          <div class="section-title">📦 NPX Command</div>
          <input id="f-npx-command" class="form-control font-monospace" placeholder="npx @playwright/mcp@latest --isolated" style="font-size:.875em">
          <div class="mt-2 text-muted" style="font-size:.8em">The MCP server started by this command handles all tool calls. The server is started on demand and kept alive between calls.</div>
        </div>

        <!-- Code box (code providers) -->
        <div class="section-box" id="code-box" style="display:none">
          <div class="section-title">🐍 Python Code</div>
          <div class="cm-label">code</div>
          <textarea id="f-code"></textarea>
        </div>

        <!-- Tools box -->
        <div class="section-box">
          <div class="section-title">
            🔧 Tools
            <button class="btn btn-sm btn-outline-secondary py-0" onclick="addTool()">+ Add Tool</button>
          </div>
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
            <div class="col-md-6">
              <div class="card wizard-choice h-100" onclick="wzSelectType('code')">
                <div class="card-body text-center p-4">
                  <div style="font-size:2.5em">🐍</div>
                  <h6 class="mt-2">Python Code</h6>
                  <small class="text-muted">Write async Python functions — each one becomes an MCP tool</small>
                </div>
              </div>
            </div>
            <div class="col-md-6">
              <div class="card wizard-choice h-100" onclick="wzSelectType('npx')">
                <div class="card-body text-center p-4">
                  <div style="font-size:2.5em">📦</div>
                  <h6 class="mt-2">NPX Package</h6>
                  <small class="text-muted">Supply an <code>npx</code> command — the server introspects the MCP tools automatically</small>
                </div>
              </div>
            </div>
          </div>
        </div>

        <!-- Step: npx command -->
        <div id="wz-npx" class="wizard-step">
          <div class="mb-3">
            <label class="form-label">Provider name</label>
            <input class="form-control" id="wz-npx-name" placeholder="playwright">
          </div>
          <div class="mb-3">
            <label class="form-label">NPX command *</label>
            <input class="form-control font-monospace" id="wz-npx-cmd" placeholder="npx @playwright/mcp@latest --isolated">
            <div class="text-muted mt-1" style="font-size:.8em">The full command used to start the MCP server process.</div>
          </div>
          <button class="btn btn-sm btn-outline-info" id="wz-introspect-btn" onclick="wzIntrospect()">🔍 Introspect Tools</button>
          <div id="wz-introspect-result" class="mt-2"></div>
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
          <button class="btn btn-sm btn-outline-info" onclick="wzAnalyze()">🔍 Analyze Functions</button>
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
let wzType = null;            // 'code' | 'npx'
let wzStep = 'type';
let wzIntrospectedTools = []; // tools returned by introspect-npx

// ─────────────────────────────────────────────────────────────────────────────
// Boot
// ─────────────────────────────────────────────────────────────────────────────
window.addEventListener('DOMContentLoaded', () => {
  codeEditor = CodeMirror.fromTextArea(document.getElementById('f-code'), {
    mode: 'python', theme: 'dracula', lineNumbers: true,
    indentWithTabs: false, indentUnit: 4, tabSize: 4,
  });
  secretsModal = new bootstrap.Modal('#secrets-modal');
  wizModal     = new bootstrap.Modal('#wizard-modal');
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
    el.innerHTML = providers.map(p => `
      <div class="provider-item ${p.name === currentName ? 'active' : ''}" onclick="openProvider('${p.name}')">
        <div style="min-width:0">
          <div class="fw-semibold">${p.name}</div>
          <small class="text-muted d-block" style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis">
            ${(p.tool_names || []).join(', ') || 'no tools'}
          </small>
        </div>
        <div class="d-flex flex-column gap-1 align-items-end ms-1 flex-shrink-0">
          <span class="${p.is_npx ? 'badge-npx' : 'badge-code'}">${p.is_npx ? 'npx' : 'code'}</span>
          <span class="badge-count">${p.tool_count}</span>
        </div>
      </div>`).join('');
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
    renderProvider(p);
    document.getElementById('empty-panel').style.display = 'none';
    document.getElementById('editor-panel').style.display = 'block';
    document.getElementById('restart-bar').style.display = 'none';
    // highlight active in list
    document.querySelectorAll('.provider-item').forEach(el => {
      el.classList.toggle('active', el.querySelector('.fw-semibold')?.textContent === name);
    });
  } catch(e) { toast(e.message, false); }
}

function renderProvider(p) {
  document.getElementById('editor-title').textContent = p.name + (p.type === 'npx' ? ' (npx)' : ' (code)');
  document.getElementById('f-documentation').value = p.documentation || '';

  const isNpx = p.type === 'npx';
  document.getElementById('npx-box').style.display = isNpx ? '' : 'none';
  document.getElementById('code-box').style.display = isNpx ? 'none' : '';

  if (isNpx) {
    document.getElementById('f-npx-command').value = p.npx_command || '';
  } else {
    codeEditor.setValue(p.code || '');
    setTimeout(() => codeEditor.refresh(), 50);
  }

  renderTools(p.tools || [], isNpx);
}

// ─────────────────────────────────────────────────────────────────────────────
// Tools rendering
// ─────────────────────────────────────────────────────────────────────────────
function renderTools(tools, isNpx) {
  const container = document.getElementById('tools-container');
  if (!tools.length) {
    container.innerHTML = '<div class="empty-state" style="padding:16px">No tools yet — click <b>+ Add Tool</b>.</div>';
    return;
  }
  container.innerHTML = tools.map((t, i) => renderToolCard(t, i, isNpx)).join('');
}

function renderToolCard(t, i, isNpx) {
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

  const fnField = isNpx ? '' : `
    <div class="mb-2">
      <label class="form-label">Function name</label>
      <input class="form-control form-control-sm font-monospace" placeholder="my_function" value="${esc(t.function||'')}"
        oninput="updateTool(${i},'function',this.value)">
    </div>`;

  const docField = `
    <div class="mb-2">
      <label class="form-label">Documentation <span class="text-muted fw-normal" style="text-transform:none">optional</span></label>
      <textarea class="form-control form-control-sm" rows="2" placeholder="Per-tool usage notes…"
        oninput="updateTool(${i},'documentation',this.value)">${esc(t.documentation||'')}</textarea>
    </div>`;

  return `
  <div class="tool-card" id="tool-card-${i}">
    <div class="tool-card-header" onclick="toggleToolCard(${i})">
      <span class="fw-semibold" id="tool-label-${i}">${esc(t.name||'(unnamed)')}</span>
      <div class="d-flex gap-2 align-items-center" onclick="event.stopPropagation()">
        <button class="btn btn-sm btn-outline-danger py-0 px-2" onclick="removeTool(${i})">Remove</button>
        <span style="color:var(--muted)">▾</span>
      </div>
    </div>
    <div class="tool-card-body" id="tool-body-${i}">
      <div class="row g-2 mb-2">
        <div class="col-md-5">
          <label class="form-label">Tool name</label>
          <input class="form-control form-control-sm font-monospace" placeholder="my_tool" value="${esc(t.name||'')}"
            oninput="updateTool(${i},'name',this.value);document.getElementById('tool-label-${i}').textContent=this.value||'(unnamed)'">
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
  if (p.type === 'npx') {
    p.npx_command = document.getElementById('f-npx-command').value.trim();
  } else {
    p.code = codeEditor.getValue();
  }
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
  const isNpx = currentProvider.type === 'npx';
  currentProvider.tools.push({
    name: '', function: '', description: '', documentation: '',
    parameters: [], secrets: [],
  });
  renderTools(currentProvider.tools, isNpx);
}

function removeTool(i) {
  ensureProvider();
  currentProvider.tools.splice(i, 1);
  renderTools(currentProvider.tools, currentProvider.type === 'npx');
}

function addParam(ti) {
  ensureProvider();
  currentProvider.tools[ti].parameters.push({name:'',type:'string',description:'',required:false,default:null});
  const isNpx = currentProvider.type === 'npx';
  renderTools(currentProvider.tools, isNpx);
}

function removeParam(ti, pi) {
  ensureProvider();
  currentProvider.tools[ti].parameters.splice(pi, 1);
  renderTools(currentProvider.tools, currentProvider.type === 'npx');
}

function addSecret(ti) {
  ensureProvider();
  currentProvider.tools[ti].secrets.push({arg:'',env:''});
  renderTools(currentProvider.tools, currentProvider.type === 'npx');
}

function removeSecret(ti, si) {
  ensureProvider();
  currentProvider.tools[ti].secrets.splice(si, 1);
  renderTools(currentProvider.tools, currentProvider.type === 'npx');
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
    loadList();
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
  // Collect all declared secret env var names from in-memory tools
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
  } catch(e) { toast(e.message, false); }
}

// ─────────────────────────────────────────────────────────────────────────────
// Wizard
// ─────────────────────────────────────────────────────────────────────────────
const WZ_STEPS = ['type','npx','code','secrets'];

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
  wzType = null; wzStep = 'type'; wzIntrospectedTools = [];
  document.getElementById('wz-npx-name').value = '';
  document.getElementById('wz-npx-cmd').value = '';
  document.getElementById('wz-code-name').value = '';
  document.getElementById('wz-code-input').value = '';
  document.getElementById('wz-introspect-result').innerHTML = '';
  document.getElementById('wz-analyze-result').innerHTML = '';
  wzShowStep('type');
  wizModal.show();
}

function wzSelectType(type) {
  wzType = type;
  document.querySelectorAll('.wizard-choice').forEach(el => el.classList.remove('selected'));
  event.currentTarget.classList.add('selected');
  setTimeout(() => wzShowStep(type), 120);
}

async function wzNext() {
  const errEl = document.getElementById('wz-error');
  errEl.textContent = '';

  if (wzStep === 'type') return; // handled by card click

  if (wzStep === 'npx') {
    const name = document.getElementById('wz-npx-name').value.trim();
    const cmd  = document.getElementById('wz-npx-cmd').value.trim();
    if (!name) { errEl.textContent = 'Provider name is required.'; return; }
    if (!cmd)  { errEl.textContent = 'NPX command is required.'; return; }
    if (!wzIntrospectedTools.length) { errEl.textContent = 'Click "Introspect Tools" first.'; return; }
    // Create the provider
    const provider = {
      name, type: 'npx', npx_command: cmd, documentation: '', code: '',
      tools: wzIntrospectedTools.map(t => ({
        name: t.name,
        function: '',
        description: t.description || '',
        documentation: '',
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

  if (wzStep === 'code') {
    const name = document.getElementById('wz-code-name').value.trim();
    const code = document.getElementById('wz-code-input').value;
    if (!name) { errEl.textContent = 'Provider name is required.'; return; }
    if (!code.trim()) { errEl.textContent = 'Code is required.'; return; }
    // Build tools from analyzed functions
    const fns = await _analyzeFns(code);
    const provider = {
      name, type: 'code', npx_command: '', documentation: '', code,
      tools: fns.map(fn => ({
        name: fn.name, function: fn.name,
        description: `TODO — describe what ${fn.name} does`,
        documentation: '',
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
  const map = {npx:'type', code:'type', secrets: wzType||'type'};
  wzShowStep(map[wzStep] || 'type');
}

async function wzIntrospect() {
  const cmd = document.getElementById('wz-npx-cmd').value.trim();
  const el  = document.getElementById('wz-introspect-result');
  const btn = document.getElementById('wz-introspect-btn');
  if (!cmd) { el.innerHTML = '<span class="text-danger">Enter an npx command first.</span>'; return; }
  btn.disabled = true; btn.textContent = '⏳ Introspecting…';
  el.innerHTML = '<span class="text-muted">Running npx — this may take a moment on first use…</span>';
  try {
    const r = await api('POST', '/api/introspect-npx', {command: cmd});
    if (!r.ok) throw new Error(r.error || 'Introspection failed');
    wzIntrospectedTools = r.tools || [];
    el.innerHTML = `<div style="color:var(--green);font-size:.875em">✓ Found ${wzIntrospectedTools.length} tool(s): <b>${wzIntrospectedTools.map(t=>t.name).join(', ')}</b></div>`;
  } catch(e) {
    wzIntrospectedTools = [];
    el.innerHTML = `<div class="text-danger" style="font-size:.875em">✗ ${e.message}</div>`;
  } finally {
    btn.disabled = false; btn.textContent = '🔍 Introspect Tools';
  }
}

async function wzAnalyze() {
  const code = document.getElementById('wz-code-input').value;
  const el   = document.getElementById('wz-analyze-result');
  try {
    const r = await api('POST', '/api/extract-functions', {code});
    if (!r.ok) { el.innerHTML = `<span class="text-danger">${r.error}</span>`; return; }
    if (!r.functions.length) { el.innerHTML = '<span class="text-warning">No <code>async def fn(context, …)</code> found.</span>'; return; }
    el.innerHTML = `<span style="color:var(--green)">✓ Found: <b>${r.functions.map(f=>f.name).join(', ')}</b></span>`;
  } catch(e) { el.innerHTML = `<span class="text-danger">${e.message}</span>`; }
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
  wizModal.hide();
  await openProvider(currentName);
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

#!/usr/bin/env bash
# tests/test_mcp_client.sh — Generic interactive MCP tool tester + Ollama summary
#
# Flow
# ────
# 1. Pick an Ollama model (menu or $OLLAMA_MODEL).
# 2. Initialize an MCP session with mcpproxy.
# 3. Show every registered tool; let you pick one.
# 4. Check that required secrets are present in .env (never prints values).
# 5. Prompt for each non-secret parameter (type-aware, required vs optional).
# 6. Call the tool — secrets are injected server-side, never by this script.
# 7. Display the result and ask Ollama to summarise it.
#
# Environment overrides:
#   MCP_URL       [http://localhost:8888/mcp]
#   UI_URL        [http://localhost:8889]      used to look up secret metadata
#   OLLAMA_URL    [http://localhost:11434]
#   OLLAMA_MODEL  skip model menu
#   ENV_FILE      [.env]                       checked for secret presence only
set -euo pipefail

MCP_URL="${MCP_URL:-http://localhost:8888/mcp}"
UI_URL="${UI_URL:-http://localhost:8889}"
OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434}"
ENV_FILE="${ENV_FILE:-.env}"

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TMP_DIR}"' EXIT

_RPC_ID=0

# ── Helpers ───────────────────────────────────────────────────────────────────

die()  { printf '\n✗  %s\n' "$*" >&2; exit 1; }
info() { printf '\n▸  %s\n' "$*"; }
ok()   { printf '✓  %s\n' "$*"; }
sep()  { printf '\n%s\n' "────────────────────────────────────────────────────"; }
bold() { printf '\033[1m%s\033[0m' "$*"; }

next_id() { _RPC_ID=$(( _RPC_ID + 1 )); printf '%d' "$_RPC_ID"; }

mcp_post() {
  # mcp_post <request_file> <response_file>
  # Sends a JSON-RPC POST; accepts both plain JSON and SSE responses.
  curl -fsS --max-time 120 \
    "${SESSION_HEADER_ARGS[@]}" \
    --data "@${1}" \
    "${MCP_URL}" > "${2}"
}

# ── Shared Python helper: unwrap SSE or plain JSON-RPC ───────────────────────
#
# Written to a temp file; sourced by heredoc Python blocks via:
#   exec(open(os.environ['_PY_LOAD_RPC']).read())
#   rpc = load_rpc('/path/to/response_file')

_PY_LOAD_RPC="${TMP_DIR}/_load_rpc.py"
cat > "${_PY_LOAD_RPC}" <<'PYHELPER'
def load_rpc(path):
    """Read a file that is plain JSON-RPC or an SSE envelope; return parsed dict."""
    import json
    from pathlib import Path
    raw = Path(path).read_text(encoding='utf-8').strip()
    if not raw:
        return {}
    # Unwrap SSE envelope  (event: message\ndata: {...}\n\n)
    if raw.startswith('event:') or raw.startswith('data:'):
        for line in raw.splitlines():
            if line.startswith('data: '):
                raw = line[6:].strip()
                break
        else:
            return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}
PYHELPER
export _PY_LOAD_RPC

# Extract + normalise the payload from a tools/call response file.
extract_tool_result() {
  local raw="$1" out="$2"
  python3 - "$raw" "$out" <<'PY'
import sys, json, os
from pathlib import Path

exec(open(os.environ['_PY_LOAD_RPC']).read())

out = Path(sys.argv[2])

def write(obj):
    out.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding='utf-8')

rpc = load_rpc(sys.argv[1])
if not rpc:
    write({'ok': False, 'error': 'Empty or unreadable response from MCP server'}); sys.exit(0)

if 'error' in rpc:
    e = rpc['error']
    write({'ok': False, 'rpc_error': e.get('message', str(e)) if isinstance(e, dict) else str(e)})
    sys.exit(0)

result  = rpc.get('result', {})
content = result.get('content', [])

if not content:
    write({'ok': True, **result} if isinstance(result, dict) else {'ok': True, 'result': result})
    sys.exit(0)

first = content[0] if isinstance(content, list) else content
if isinstance(first, dict) and first.get('type') == 'text':
    try:
        write(json.loads(first['text']))
    except (json.JSONDecodeError, TypeError):
        write({'ok': True, 'text': first['text']})
else:
    write({'ok': True, 'content': content})
PY
}

# ── Step 1: Read .env key names — never values ────────────────────────────────

ENV_KEYS_FILE="${TMP_DIR}/env_keys.txt"
python3 - "${ENV_FILE}" "${ENV_KEYS_FILE}" <<'PY'
import sys
from pathlib import Path
env_file = Path(sys.argv[1])
keys = []
if env_file.exists():
    for line in env_file.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            key = line.split('=', 1)[0].strip()
            if key:
                keys.append(key)
Path(sys.argv[2]).write_text('\n'.join(keys), encoding='utf-8')
PY

# ── Step 2: Ollama model selection ────────────────────────────────────────────

sep
printf 'MCP Interactive Tool Tester\n'
sep
printf '  MCP    : %s\n' "${MCP_URL}"
printf '  UI     : %s\n' "${UI_URL}"
printf '  Ollama : %s\n' "${OLLAMA_URL}"

printf '\n  Checking Ollama… '
TAGS_JSON="$(curl -fsS --max-time 5 "${OLLAMA_URL}/api/tags")" \
  || die "Ollama not reachable at ${OLLAMA_URL}.  Is it running?"
printf 'OK\n'

mapfile -t MODELS < <(printf '%s' "${TAGS_JSON}" | python3 -c "
import json, sys
for m in json.load(sys.stdin).get('models', []):
    print(m['name'])
")
[[ ${#MODELS[@]} -gt 0 ]] || die "No models found.  Pull one:  ollama pull qwen3:14b"

if [[ -n "${OLLAMA_MODEL:-}" ]]; then
  FOUND=0
  for m in "${MODELS[@]}"; do [[ "$m" == "${OLLAMA_MODEL}" ]] && FOUND=1 && break; done
  if [[ "${FOUND}" == "1" ]]; then
    printf '\n  Model (env): %s\n' "${OLLAMA_MODEL}"
  else
    printf '\n  ⚠  OLLAMA_MODEL="%s" not found — showing menu.\n' "${OLLAMA_MODEL}"
    unset OLLAMA_MODEL
  fi
fi

if [[ -z "${OLLAMA_MODEL:-}" ]]; then
  printf '\n  Available models:\n'
  for i in "${!MODELS[@]}"; do
    printf '    %2d)  %s\n' "$((i+1))" "${MODELS[$i]}"
  done
  if [[ ${#MODELS[@]} -eq 1 ]]; then
    OLLAMA_MODEL="${MODELS[0]}"
    printf '\n  Auto-selected: %s\n' "${OLLAMA_MODEL}"
  else
    while true; do
      printf '\n  Select model [1-%d]: ' "${#MODELS[@]}"
      read -r SEL
      if [[ "$SEL" =~ ^[0-9]+$ ]] && (( SEL >= 1 && SEL <= ${#MODELS[@]} )); then
        OLLAMA_MODEL="${MODELS[$((SEL-1))]}"
        break
      fi
      printf '  Invalid choice.\n'
    done
  fi
fi

# ── Step 3: MCP initialize ────────────────────────────────────────────────────

sep
printf '  Checking MCP server… '

INIT_REQ="${TMP_DIR}/init_req.json"
INIT_RESP="${TMP_DIR}/init_resp.json"
HEADERS_FILE="${TMP_DIR}/headers.txt"

cat > "${INIT_REQ}" <<'JSON'
{
  "jsonrpc": "2.0", "id": 1,
  "method": "initialize",
  "params": {
    "protocolVersion": "2024-11-05",
    "capabilities": {},
    "clientInfo": {"name": "test-mcp-client", "version": "0.1"}
  }
}
JSON

curl -fsS --max-time 10 \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  --data "@${INIT_REQ}" \
  -D "${HEADERS_FILE}" \
  "${MCP_URL}" > "${INIT_RESP}" \
  || die "MCP server not reachable at ${MCP_URL}\n  Start it:  ./run_local.sh"
printf 'OK\n'

_RPC_ID=1
SESSION_ID="$(awk 'BEGIN{IGNORECASE=1} /^mcp-session-id:/ {gsub("\r","", $2); print $2}' \
  "${HEADERS_FILE}" | tail -n 1)"

SESSION_HEADER_ARGS=(
  -H 'Content-Type: application/json'
  -H 'Accept: application/json, text/event-stream'
)
[[ -n "${SESSION_ID}" ]] && SESSION_HEADER_ARGS+=(-H "Mcp-Session-Id: ${SESSION_ID}")
printf '  Session: %s\n' "${SESSION_ID:-(stateless)}"

# ── Step 4: tools/list + tool selection ──────────────────────────────────────

sep
TOOLS_REQ="${TMP_DIR}/tools_req.json"
TOOLS_RESP="${TMP_DIR}/tools_resp.json"

printf '{"jsonrpc":"2.0","id":%d,"method":"tools/list","params":{}}\n' "$(next_id)" \
  > "${TOOLS_REQ}"
mcp_post "${TOOLS_REQ}" "${TOOLS_RESP}"

# Write tool names + descriptions to a TSV; handles SSE or plain JSON.
TOOLS_TSV="${TMP_DIR}/tools.tsv"
python3 - "${TOOLS_RESP}" "${TOOLS_TSV}" <<'PY'
import json, sys, os
from pathlib import Path

exec(open(os.environ['_PY_LOAD_RPC']).read())

rpc   = load_rpc(sys.argv[1])
tools = rpc.get('result', {}).get('tools', [])
if not tools:
    print("no_tools", flush=True)
    sys.exit(0)
with open(sys.argv[2], 'w', encoding='utf-8') as f:
    for t in tools:
        name = t.get('name', '')
        desc = t.get('description', '').replace('\n', ' ').replace('\t', ' ')[:72]
        f.write(f"{name}\t{desc}\n")
PY

[[ -s "${TOOLS_TSV}" ]] || die "No tools registered in mcpproxy."

mapfile -t TOOL_NAMES < <(cut -f1 "${TOOLS_TSV}")
mapfile -t TOOL_DESCS < <(cut -f2 "${TOOLS_TSV}")

printf '\n  Registered tools:\n\n'
for i in "${!TOOL_NAMES[@]}"; do
  printf '  %3d)  ' "$((i+1))"
  bold "${TOOL_NAMES[$i]}"
  printf '\n        %s\n' "${TOOL_DESCS[$i]}"
done

while true; do
  printf '\n  Select tool [1-%d]: ' "${#TOOL_NAMES[@]}"
  read -r SEL
  if [[ "$SEL" =~ ^[0-9]+$ ]] && (( SEL >= 1 && SEL <= ${#TOOL_NAMES[@]} )); then
    SELECTED_TOOL="${TOOL_NAMES[$((SEL-1))]}"
    break
  fi
  printf '  Invalid choice.\n'
done

sep
printf '  Selected: '; bold "${SELECTED_TOOL}"; printf '\n'

# ── Step 5: Secret status check ───────────────────────────────────────────────
# Looks up the provider that owns this tool via the UI API, then checks
# which declared secrets ARE and ARE NOT present in .env.
# Only key names are compared — values are never read or printed.

python3 - "${SELECTED_TOOL}" "${UI_URL}" "${ENV_KEYS_FILE}" <<'PY'
import json, sys, urllib.request
from pathlib import Path

tool_name  = sys.argv[1]
ui_url     = sys.argv[2]
env_keys   = set(Path(sys.argv[3]).read_text(encoding='utf-8').split())

try:
    with urllib.request.urlopen(f"{ui_url}/api/tools", timeout=5) as r:
        providers = json.load(r)
except Exception:
    providers = []

for p in providers:
    if tool_name in (p.get('tool_names') or []):
        sk = p.get('secret_keys') or []
        if sk:
            print(f"\n  Secrets for provider '{p['name']}' (injected server-side):")
            all_ok = True
            for k in sk:
                if k in env_keys:
                    print(f"    ✓  {k}  — set in .env")
                else:
                    print(f"    ✗  {k}  — NOT SET (tool call may fail)")
                    all_ok = False
            if not all_ok:
                print(f"\n  Add missing values to .env or use the Secrets manager at {ui_url}")
        break
PY

# ── Step 6: Parameter prompting ───────────────────────────────────────────────

sep
info "Parameters for '${SELECTED_TOOL}'"
printf '\n  (* = required)\n'

# Pre-seed with [] so even if the parsing block fails, the file is valid JSON.
PARAMS_FILE="${TMP_DIR}/params.json"
printf '[]' > "${PARAMS_FILE}"

python3 - "${SELECTED_TOOL}" "${TOOLS_RESP}" "${PARAMS_FILE}" <<'PY'
import json, sys, os
from pathlib import Path

exec(open(os.environ['_PY_LOAD_RPC']).read())

tool_name = sys.argv[1]
rpc       = load_rpc(sys.argv[2])
tools     = rpc.get('result', {}).get('tools', [])
tool      = next((t for t in tools if t['name'] == tool_name), None)

if not tool:
    Path(sys.argv[3]).write_text('[]', encoding='utf-8')
    sys.exit(0)

schema = tool.get('inputSchema') or tool.get('input_schema') or {}
props  = schema.get('properties', {})
req    = set(schema.get('required', []))
params = []
for name, defn in props.items():
    default = defn.get('default')
    # Coerce non-JSON-serializable defaults (e.g. datetime.date from YAML) to strings
    try:
        json.dumps(default)
    except (TypeError, ValueError):
        default = str(default)
    params.append({
        'name':        name,
        'type':        defn.get('type', 'string'),
        'description': defn.get('description', ''),
        'required':    name in req,
        'default':     default,
    })

Path(sys.argv[3]).write_text(
    json.dumps(params, indent=2, ensure_ascii=False),
    encoding='utf-8'
)
PY

# Read params from the file — use try/except so process-substitution failures
# produce empty arrays instead of bare tracebacks.
mapfile -t PNAMES < <(python3 -c "
import json, sys
try:
    [print(x['name']) for x in json.load(open('${PARAMS_FILE}'))]
except Exception as e:
    print(f'# error: {e}', file=sys.stderr)
")
mapfile -t PTYPES < <(python3 -c "
import json, sys
try:
    [print(x.get('type','string')) for x in json.load(open('${PARAMS_FILE}'))]
except Exception: pass
")
mapfile -t PDESCS < <(python3 -c "
import json, sys
try:
    [print(x.get('description','')) for x in json.load(open('${PARAMS_FILE}'))]
except Exception: pass
")
mapfile -t PREQS  < <(python3 -c "
import json, sys
try:
    [print('1' if x.get('required') else '0') for x in json.load(open('${PARAMS_FILE}'))]
except Exception: pass
")
mapfile -t PDEFS  < <(python3 -c "
import json, sys
try:
    for x in json.load(open('${PARAMS_FILE}')):
        d = x.get('default')
        print('' if d is None else str(d))
except Exception: pass
")

if [[ ${#PNAMES[@]} -eq 0 ]]; then
  printf '\n  No parameters — secrets will be injected server-side.\n'
fi

# Collect values into a TSV written to disk (no env-var leakage)
VALS_TSV="${TMP_DIR}/values.tsv"
> "${VALS_TSV}"

for i in "${!PNAMES[@]}"; do
  pname="${PNAMES[$i]}"
  ptype="${PTYPES[$i]:-string}"
  pdesc="${PDESCS[$i]:-}"
  preq="${PREQS[$i]:-0}"
  pdef="${PDEFS[$i]:-}"

  # Skip internal error markers
  [[ "${pname}" == "# error:"* ]] && continue

  printf '\n  '
  bold "${pname}"
  printf '  \033[2m(%s)\033[0m' "${ptype}"
  [[ "${preq}" == "1" ]] && printf '  \033[31m*\033[0m'
  printf '\n'
  [[ -n "${pdesc}" ]] && printf '  %s\n' "${pdesc}"

  if [[ "${ptype}" == "boolean" ]]; then
    [[ -n "${pdef}" ]] && PROMPT="  [y/n, default ${pdef}]: " \
                       || PROMPT="  [y/n]: "
    while true; do
      printf '%s' "${PROMPT}"
      read -r VAL
      [[ -z "${VAL}" ]] && VAL="${pdef}"
      case "${VAL,,}" in
        y|yes|true|1)
          printf '%s\ttrue\n'  "${pname}" >> "${VALS_TSV}"; break ;;
        n|no|false|0)
          printf '%s\tfalse\n' "${pname}" >> "${VALS_TSV}"; break ;;
        '')
          [[ "${preq}" == "0" ]] && break || printf '  Required — enter y or n: ' ;;
        *)
          printf '  Enter y or n: ' ;;
      esac
    done
  else
    if [[ -n "${pdef}" ]]; then
      printf '  Value [%s]: ' "${pdef}"
    elif [[ "${preq}" == "0" ]]; then
      printf '  Value (optional — press Enter to skip): '
    else
      printf '  Value: '
    fi

    VALUE=""
    while true; do
      read -r VALUE
      [[ -z "${VALUE}" ]] && VALUE="${pdef}"
      if [[ -z "${VALUE}" && "${preq}" == "1" ]]; then
        printf '  Required — enter a value: '
      else
        break
      fi
    done
    [[ -n "${VALUE}" ]] && printf '%s\t%s\n' "${pname}" "${VALUE}" >> "${VALS_TSV}"
  fi
done

# ── Step 7: Build typed JSON arguments ────────────────────────────────────────

# Pre-seed with {} so downstream step never sees an empty file.
ARGS_FILE="${TMP_DIR}/call_args.json"
printf '{}' > "${ARGS_FILE}"

python3 - "${PARAMS_FILE}" "${VALS_TSV}" "${ARGS_FILE}" <<'PY'
import json, sys
from pathlib import Path

try:
    params = json.load(open(sys.argv[1]))
except Exception:
    params = []
tsv_raw = Path(sys.argv[2]).read_text(encoding='utf-8') if Path(sys.argv[2]).exists() else ''

# Map name → raw string value
raw_vals = {}
for line in tsv_raw.strip().splitlines():
    if '\t' in line:
        name, _, val = line.partition('\t')
        raw_vals[name.strip()] = val.strip()

# Apply defaults for optional params the user skipped
param_map = {p['name']: p for p in params}
for name, p in param_map.items():
    if name not in raw_vals and not p.get('required') and p.get('default') is not None:
        raw_vals[name] = str(p['default'])

# Type-coerce each value
args = {}
for name, raw in raw_vals.items():
    p     = param_map.get(name)
    ptype = p.get('type', 'string') if p else 'string'
    if ptype == 'integer':
        try: args[name] = int(raw)
        except ValueError: args[name] = raw
    elif ptype == 'number':
        try: args[name] = float(raw)
        except ValueError: args[name] = raw
    elif ptype == 'boolean':
        args[name] = raw.lower() in ('true', '1', 'yes', 'y')
    else:
        args[name] = raw

Path(sys.argv[3]).write_text(json.dumps(args, indent=2), encoding='utf-8')
PY

# ── Step 8: Call the tool ─────────────────────────────────────────────────────

sep
printf '  Calling '; bold "${SELECTED_TOOL}"; printf '…\n'
python3 -c "
import json, sys
try:
    args = json.load(open('${ARGS_FILE}'))
    if args:
        print(f'  Arguments: {json.dumps(args, indent=2)}')
    else:
        print('  (no arguments — secrets injected server-side)')
except Exception as e:
    print(f'  (could not read args: {e})', file=sys.stderr)
"

CALL_REQ="${TMP_DIR}/call_req.json"
CALL_RAW="${TMP_DIR}/call_raw.json"
CALL_RESULT="${TMP_DIR}/call_result.json"

python3 - "${SELECTED_TOOL}" "${ARGS_FILE}" "${CALL_REQ}" "$(next_id)" <<'PY'
import json, sys
from pathlib import Path
tool = sys.argv[1]
try:
    args = json.load(open(sys.argv[2]))
except Exception:
    args = {}
rid = int(sys.argv[4])
req = {"jsonrpc": "2.0", "id": rid, "method": "tools/call",
       "params": {"name": tool, "arguments": args}}
Path(sys.argv[3]).write_text(json.dumps(req, indent=2), encoding='utf-8')
PY

mcp_post "${CALL_REQ}" "${CALL_RAW}"
extract_tool_result "${CALL_RAW}" "${CALL_RESULT}"

sep
printf '  Result:\n\n'
python3 -c "
import json, sys
try:
    r = json.load(open('${CALL_RESULT}'))
    print(json.dumps(r, indent=2, ensure_ascii=False))
except Exception as e:
    print(f'(could not parse result: {e})', file=sys.stderr)
"

# ── Step 9: Ollama summary ────────────────────────────────────────────────────

sep
info "Asking Ollama to summarise the result"

OLLAMA_PAYLOAD="${TMP_DIR}/ollama_payload.json"
OLLAMA_RESP="${TMP_DIR}/ollama_resp.json"

python3 - "${SELECTED_TOOL}" "${CALL_RESULT}" "${OLLAMA_PAYLOAD}" "${OLLAMA_MODEL}" <<'PY'
import json, sys
from pathlib import Path

tool   = sys.argv[1]
result = Path(sys.argv[2]).read_text(encoding='utf-8')
out    = Path(sys.argv[3])
model  = sys.argv[4]

prompt = (
    f"You are reviewing the output of an MCP tool call.\n\n"
    f"Tool: {tool}\n\n"
    f"Result:\n{result}\n\n"
    "Summarise what was returned in plain language. "
    "If it indicates an error, explain what went wrong and suggest a fix. "
    "Be concise."
)
out.write_text(
    json.dumps({"model": model, "prompt": prompt, "stream": False}),
    encoding='utf-8'
)
PY

curl -fsS --max-time 300 \
  -H 'Content-Type: application/json' \
  --data "@${OLLAMA_PAYLOAD}" \
  "${OLLAMA_URL}/api/generate" > "${OLLAMA_RESP}"

printf '\n'
python3 -c "
import json, sys
try:
    r = json.load(open('${OLLAMA_RESP}', encoding='utf-8'))
    print(r.get('response', r))
except Exception as e:
    print(f'(could not read Ollama response: {e})', file=sys.stderr)
"

sep

# ── Step 10: List files in the mcpproxy files directory ───────────────────────

info "Listing files produced in the mcpproxy files directory"

FILES_DATA="${TMP_DIR}/files_data.json"

# One Python block handles all MCP calls (listfiles + one getfile per entry)
# so we avoid bash escaping issues with arbitrary file names.
python3 - "${MCP_URL}" "${SESSION_ID:-}" "$(( _RPC_ID + 1 ))" "${FILES_DATA}" <<'PY'
import json, sys, urllib.request, urllib.error
from pathlib import Path

mcp_url    = sys.argv[1]
session_id = sys.argv[2]
rpc_id     = int(sys.argv[3])
out_path   = Path(sys.argv[4])


def _mcp_call(method, params):
    global rpc_id
    rid = rpc_id
    rpc_id += 1
    headers = {
        'Content-Type': 'application/json',
        'Accept': 'application/json, text/event-stream',
    }
    if session_id:
        headers['Mcp-Session-Id'] = session_id
    body = json.dumps(
        {'jsonrpc': '2.0', 'id': rid, 'method': method, 'params': params}
    ).encode()
    req = urllib.request.Request(mcp_url, data=body, headers=headers, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode('utf-8')
    except urllib.error.HTTPError as exc:
        return {'error': {'code': exc.code, 'message': str(exc)}}
    # Unwrap SSE envelope (event: message\ndata: {...}\n\n)
    if raw.lstrip().startswith(('event:', 'data:')):
        for line in raw.splitlines():
            if line.startswith('data: '):
                raw = line[6:].strip()
                break
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _extract(rpc):
    """Unwrap a tools/call JSON-RPC response to its payload dict."""
    if 'error' in rpc:
        e = rpc['error']
        msg = e.get('message', str(e)) if isinstance(e, dict) else str(e)
        return {'ok': False, 'error': msg}
    result  = rpc.get('result', {})
    content = result.get('content', [])
    if not content:
        return result if isinstance(result, dict) else {'raw': result}
    first = content[0] if isinstance(content, list) else content
    if isinstance(first, dict) and first.get('type') == 'text':
        try:
            return json.loads(first['text'])
        except (json.JSONDecodeError, TypeError):
            return {'text': first['text']}
    return {'content': content}


# 1. Call mcpproxy-listfiles (root directory)
listing = _extract(_mcp_call('tools/call', {'name': 'mcpproxy-listfiles', 'arguments': {}}))
entries  = listing.get('entries', [])
base_dir = listing.get('base_dir', '.playwright-mcp')

# 2. Fetch every file entry
files_fetched = []
for entry in entries:
    if entry.get('type') != 'file':
        continue
    fname       = entry['name']
    file_result = _extract(
        _mcp_call('tools/call', {'name': 'mcpproxy-getfile', 'arguments': {'path': fname}})
    )
    files_fetched.append({
        'name':     fname,
        'size':     entry.get('size'),
        'ok':       file_result.get('ok', True),
        'encoding': file_result.get('encoding', 'text'),
        'content':  file_result.get('content', ''),
        'error':    file_result.get('error', ''),
    })

out_path.write_text(
    json.dumps({
        'ok':       listing.get('ok', True),
        'base_dir': base_dir,
        'entries':  entries,
        'files':    files_fetched,
    }, indent=2, ensure_ascii=False),
    encoding='utf-8',
)
PY

# Display listing + fetch status
printf '\n'
python3 -c "
import json, sys
try:
    d = json.load(open('${FILES_DATA}', encoding='utf-8'))
    base_dir = d.get('base_dir', '.playwright-mcp')
    entries  = d.get('entries',  [])
    files    = d.get('files',    [])

    if not entries:
        print(f'  (no files found in {base_dir})')
    else:
        print(f'  Base directory: {base_dir}')
        print()
        for e in entries:
            icon = '📁' if e.get('type') == 'directory' else '📄'
            size = f\" ({e['size']} bytes)\" if e.get('size') is not None else ''
            print(f\"  {icon}  {e['name']}{size}\")
        print()
        for f in files:
            if f.get('error'):
                print(f\"  ✗  {f['name']}  — {f['error']}\")
            elif f.get('encoding') == 'base64':
                print(f\"  ✓  {f['name']}  [binary, {f.get('size','?')} bytes]\")
            else:
                preview = f.get('content','')[:80].replace('\n',' ')
                ellipsis = '…' if len(f.get('content','')) > 80 else ''
                print(f\"  ✓  {f['name']}  →  {preview}{ellipsis}\")
except Exception as e:
    print(f'  (could not display file data: {e})', file=sys.stderr)
"

# ── Step 11: Ollama summary of file contents ──────────────────────────────────

HAS_FILES="$(python3 -c "
import json
try:
    d = json.load(open('${FILES_DATA}', encoding='utf-8'))
    print('yes' if d.get('files') else 'no')
except Exception:
    print('no')
")"

if [[ "${HAS_FILES}" == "yes" ]]; then

  sep
  info "Asking Ollama to summarise the file contents"

  OLLAMA_FILES_PAYLOAD="${TMP_DIR}/ollama_files_payload.json"
  OLLAMA_FILES_RESP="${TMP_DIR}/ollama_files_resp.json"

  python3 - "${SELECTED_TOOL}" "${CALL_RESULT}" "${FILES_DATA}" \
            "${OLLAMA_FILES_PAYLOAD}" "${OLLAMA_MODEL}" <<'PY'
import json, sys
from pathlib import Path

tool         = sys.argv[1]
orig_result  = Path(sys.argv[2]).read_text(encoding='utf-8')
files_data   = json.loads(Path(sys.argv[3]).read_text(encoding='utf-8'))
out          = Path(sys.argv[4])
model        = sys.argv[5]

base_dir = files_data.get('base_dir', '.playwright-mcp')
files    = files_data.get('files', [])

sections = []
for f in files:
    fname = f['name']
    if f.get('error'):
        sections.append(f"--- {fname} (error: {f['error']}) ---")
    elif f.get('encoding') == 'base64':
        sections.append(
            f"--- {fname} [binary file, {f.get('size', '?')} bytes — content omitted] ---"
        )
    else:
        content = f.get('content', '')
        if len(content) > 4000:
            content = content[:4000] + '\n…[truncated]'
        sections.append(f"--- {fname} ---\n{content}")

files_block = '\n\n'.join(sections) if sections else 'No files retrieved.'

prompt = (
    f"You just called the MCP tool '{tool}'.\n\n"
    f"Original tool result:\n{orig_result}\n\n"
    f"The following files were found in the mcpproxy files directory ({base_dir}) "
    f"and retrieved for you:\n\n"
    f"{files_block}\n\n"
    "Please summarise what was produced. For each file, describe its contents or "
    "purpose. For binary files (e.g. PNG screenshots), note their presence and size. "
    "For text files (JSON snapshots, HTML, logs, etc.), describe what they contain. "
    "Relate the files back to the tool call where relevant. Be concise."
)

out.write_text(
    json.dumps({"model": model, "prompt": prompt, "stream": False}),
    encoding='utf-8',
)
PY

  curl -fsS --max-time 300 \
    -H 'Content-Type: application/json' \
    --data "@${OLLAMA_FILES_PAYLOAD}" \
    "${OLLAMA_URL}/api/generate" > "${OLLAMA_FILES_RESP}"

  printf '\n'
  python3 -c "
import json
try:
    r = json.load(open('${OLLAMA_FILES_RESP}', encoding='utf-8'))
    print(r.get('response', r))
except Exception as e:
    print(f'(could not read Ollama response: {e})', file=sys.stderr)
"

else
  printf '\n  No files found — nothing to summarise.\n'
fi

sep
ok "Done."
printf '\n'

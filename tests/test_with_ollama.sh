#!/usr/bin/env bash
# tests/test_with_ollama.sh — Run a quick end-to-end test of the MCP server using
# a locally running Ollama instance.
#
# Flow
# ────
# 1. Query Ollama for available models and let the user pick one.
# 2. Send MCP initialize → tools/list (→ tools/call if RUN_REAL_TOOL=1).
# 3. Print the URL, request body, and response body for every MCP call.
# 4. Ask Ollama to summarise the tool list and the call result.
#
# Usage (from project root):
#   bash tests/test_with_ollama.sh
#   MCP_URL=http://localhost:8888/mcp bash tests/test_with_ollama.sh
#   OLLAMA_MODEL=qwen3:14b bash tests/test_with_ollama.sh
#   RUN_REAL_TOOL=1 bash tests/test_with_ollama.sh
set -euo pipefail

MCP_URL="${MCP_URL:-http://localhost:8888/mcp}"
OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434}"

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TMP_DIR}"' EXIT

INIT_RESPONSE="${TMP_DIR}/initialize.json"
TOOLS_RESPONSE="${TMP_DIR}/tools_list.json"
CALL_RESPONSE="${TMP_DIR}/tool_call.json"
OLLAMA_PAYLOAD="${TMP_DIR}/ollama_payload.json"
OLLAMA_RESPONSE="${TMP_DIR}/ollama_response.json"
HEADERS_FILE="${TMP_DIR}/headers.txt"

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

pretty_json() {
  local file="$1"
  python3 -m json.tool --no-ensure-ascii "${file}" 2>/dev/null || cat "${file}"
}

mcp_call() {
  local label="$1" req_file="$2" resp_file="$3"
  shift 3

  printf '\n══════════════════════════════════════════════════\n'
  printf '  MCP ▸ %s\n' "${label}"
  printf '  URL: %s\n' "${MCP_URL}"
  printf '  ── request ────────────────────────────────────\n'
  pretty_json "${req_file}"
  printf '\n'

  curl -fsS "$@" \
    --data "@${req_file}" \
    "${MCP_URL}" > "${resp_file}"

  printf '  ── response ───────────────────────────────────\n'
  pretty_json "${resp_file}"
  printf '\n'
}

# ─────────────────────────────────────────────────────────────────────────────
# Step 1 — Model selection
# ─────────────────────────────────────────────────────────────────────────────

printf 'Checking Ollama endpoint: %s\n' "${OLLAMA_URL}"
TAGS_JSON="$(curl -fsS "${OLLAMA_URL}/api/tags")" \
  || { printf '✗ Ollama is not reachable at %s\n' "${OLLAMA_URL}" >&2; exit 1; }

mapfile -t MODELS < <(printf '%s' "${TAGS_JSON}" | python3 -c "
import json, sys
data = json.load(sys.stdin)
for m in data.get('models', []):
    print(m['name'])
")

if [[ ${#MODELS[@]} -eq 0 ]]; then
  printf '✗ No models found in Ollama.\n' >&2
  printf '  Pull one first, e.g.:  ollama pull qwen3:14b\n' >&2
  exit 1
fi

if [[ -n "${OLLAMA_MODEL:-}" ]]; then
  FOUND=0
  for m in "${MODELS[@]}"; do
    [[ "$m" == "$OLLAMA_MODEL" ]] && FOUND=1 && break
  done
  if [[ "$FOUND" == "1" ]]; then
    printf 'Using model from environment: %s\n' "${OLLAMA_MODEL}"
  else
    printf '⚠  OLLAMA_MODEL="%s" is not available; please choose from the menu.\n\n' "${OLLAMA_MODEL}"
    unset OLLAMA_MODEL
  fi
fi

if [[ -z "${OLLAMA_MODEL:-}" ]]; then
  printf '\nAvailable Ollama models:\n'
  for i in "${!MODELS[@]}"; do
    printf '  %2d)  %s\n' "$((i + 1))" "${MODELS[$i]}"
  done

  if [[ ${#MODELS[@]} -eq 1 ]]; then
    OLLAMA_MODEL="${MODELS[0]}"
    printf '\nOnly one model available — auto-selected: %s\n' "${OLLAMA_MODEL}"
  else
    while true; do
      printf '\nSelect model [1-%d]: ' "${#MODELS[@]}"
      read -r SEL
      if [[ "$SEL" =~ ^[0-9]+$ ]] && (( SEL >= 1 && SEL <= ${#MODELS[@]} )); then
        OLLAMA_MODEL="${MODELS[$((SEL - 1))]}"
        break
      fi
      printf '  Invalid — enter a number between 1 and %d.\n' "${#MODELS[@]}"
    done
  fi
fi

printf '\nMCP endpoint : %s\n' "${MCP_URL}"
printf 'Ollama model : %s\n' "${OLLAMA_MODEL}"

# ─────────────────────────────────────────────────────────────────────────────
# Step 2 — MCP: initialize
# ─────────────────────────────────────────────────────────────────────────────

cat > "${TMP_DIR}/initialize_request.json" <<'JSON'
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "initialize",
  "params": {
    "protocolVersion": "2025-06-18",
    "capabilities": {},
    "clientInfo": {
      "name": "local-ollama-shell-test",
      "version": "0.1.0"
    }
  }
}
JSON

mcp_call "initialize" \
  "${TMP_DIR}/initialize_request.json" \
  "${INIT_RESPONSE}" \
  -D "${HEADERS_FILE}" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream'

SESSION_ID="$(awk 'BEGIN{IGNORECASE=1} /^mcp-session-id:/ {gsub("\r","", $2); print $2}' \
  "${HEADERS_FILE}" | tail -n 1)"

HEADER_ARGS=(-H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream')
if [[ -n "${SESSION_ID}" ]]; then
  HEADER_ARGS+=(-H "Mcp-Session-Id: ${SESSION_ID}")
  printf 'MCP session id: %s\n' "${SESSION_ID}"
else
  printf 'No MCP session id — continuing stateless.\n'
fi

# ─────────────────────────────────────────────────────────────────────────────
# Step 3 — MCP: tools/list
# ─────────────────────────────────────────────────────────────────────────────

cat > "${TMP_DIR}/tools_list_request.json" <<'JSON'
{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "tools/list",
  "params": {}
}
JSON

mcp_call "tools/list" \
  "${TMP_DIR}/tools_list_request.json" \
  "${TOOLS_RESPONSE}" \
  "${HEADER_ARGS[@]}"

# ─────────────────────────────────────────────────────────────────────────────
# Step 4 — MCP: tools/call  (or synthetic fallback)
# ─────────────────────────────────────────────────────────────────────────────

RUN_REAL_TOOL="${RUN_REAL_TOOL:-0}"

if [[ "${RUN_REAL_TOOL}" == "1" ]]; then
  # Extract the first tool name from the tools/list response to call generically
  FIRST_TOOL="$(python3 -c "
import json, sys
r = json.load(open('${TOOLS_RESPONSE}'))
tools = r.get('result', {}).get('tools', [])
print(tools[0]['name'] if tools else '')
")"

  if [[ -n "$FIRST_TOOL" ]]; then
    cat > "${TMP_DIR}/tool_call_request.json" <<JSON
{
  "jsonrpc": "2.0",
  "id": 3,
  "method": "tools/call",
  "params": {
    "name": "${FIRST_TOOL}",
    "arguments": {}
  }
}
JSON
    mcp_call "tools/call (${FIRST_TOOL})" \
      "${TMP_DIR}/tool_call_request.json" \
      "${CALL_RESPONSE}" \
      "${HEADER_ARGS[@]}" || true
  fi
else
  printf '\n══════════════════════════════════════════════════\n'
  printf '  MCP ▸ tools/call  (SKIPPED — set RUN_REAL_TOOL=1 to run)\n'
  printf '  URL: %s\n' "${MCP_URL}"
  printf '\n'

  cat > "${CALL_RESPONSE}" <<'JSON'
{"note": "RUN_REAL_TOOL was not set to 1; no tool was invoked."}
JSON
fi

# ─────────────────────────────────────────────────────────────────────────────
# Step 5 — Ollama summary
# ─────────────────────────────────────────────────────────────────────────────

python3 - "${TOOLS_RESPONSE}" "${CALL_RESPONSE}" "${OLLAMA_PAYLOAD}" "${OLLAMA_MODEL}" <<'PY'
import json, sys
from pathlib import Path

tools = Path(sys.argv[1]).read_text(encoding="utf-8")
call  = Path(sys.argv[2]).read_text(encoding="utf-8")
out   = Path(sys.argv[3])
model = sys.argv[4]

prompt = f"""
You are testing a local MCP server. Summarise the tools that are exposed,
confirm that secrets do NOT appear in the tool argument schemas, and explain
any two-factor fallback behaviour shown in the sample. Be concise.

MCP tools/list response:
{tools}

Tool call or fallback sample:
{call}
"""

out.write_text(json.dumps({"model": model, "prompt": prompt, "stream": False}),
               encoding="utf-8")
PY

printf '\n══════════════════════════════════════════════════\n'
printf '  Ollama ▸ /api/generate\n'
printf '  URL  : %s/api/generate\n' "${OLLAMA_URL}"
printf '  Model: %s\n' "${OLLAMA_MODEL}"
printf '\n'

curl -fsS \
  -H 'Content-Type: application/json' \
  --data "@${OLLAMA_PAYLOAD}" \
  "${OLLAMA_URL}/api/generate" > "${OLLAMA_RESPONSE}"

printf 'Ollama summary:\n\n'
python3 -c "
import json, sys
r = json.load(open('${OLLAMA_RESPONSE}', encoding='utf-8'))
print(r.get('response', r))
"

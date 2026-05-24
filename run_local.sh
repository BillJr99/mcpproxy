#!/usr/bin/env bash
# run_local.sh — Configure and start the MCP server locally.
#
# What this script does
# ─────────────────────
# 1. If .env.example is missing, generate it from tools/*.yaml.
# 2. Collect all required env vars (union of .env.example + YAML secrets.env).
# 3. Prompt interactively for any that are missing or still at placeholder values.
#    Secret vars (password, token, …) use hidden input.
# 4. Write / update .env  (Docker also reads this file via env_file in compose).
# 5. Override MCP_TOOL_CONFIG_DIR with the correct local path so the server
#    can find the YAML files without Docker's /app prefix.
# 6. Create / activate .venv, install deps, start the server.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$SCRIPT_DIR"
cd "$ROOT_DIR"

ENV_FILE="$ROOT_DIR/.env"
ENV_EXAMPLE="$ROOT_DIR/.env.example"
TOOLS_CONFIG_DIR="$ROOT_DIR/tools"

# ─────────────────────────────────────────────────────────────────────────────
# Utilities
# ─────────────────────────────────────────────────────────────────────────────

die()  { printf '\n✗  %s\n' "$*" >&2; exit 1; }
info() { printf '→  %s\n' "$*"; }
ok()   { printf '✓  %s\n' "$*"; }

python3 --version >/dev/null 2>&1 || die "python3 is required but was not found in PATH."

# ─────────────────────────────────────────────────────────────────────────────
# Step 1 — Generate .env.example if it doesn't exist
# ─────────────────────────────────────────────────────────────────────────────

if [[ ! -f "$ENV_EXAMPLE" ]]; then
  info ".env.example not found — generating from tools/*.yaml"

  python3 - "$TOOLS_CONFIG_DIR" "$ENV_EXAMPLE" <<'PY'
import sys
from pathlib import Path

tools_dir = Path(sys.argv[1])
out_path  = Path(sys.argv[2])

try:
    import yaml
except ImportError:
    yaml = None

# Ordered dict: var_name -> (default, comment)
entries = {}

def add(name, default, comment):
    if name not in entries:
        entries[name] = (default, comment)

# Standard server vars (always included)
add("MCP_SERVER_NAME",
    "mcpproxy",
    "Display name reported by the MCP server")
add("MCP_TOOL_CONFIG_DIR",
    "/app/tools",
    "Path to tool YAML directory — Docker: /app/tools, Local: overridden by run_local.sh")

# Secrets discovered from tool YAML files
if yaml and tools_dir.exists():
    for yf in sorted(tools_dir.glob("*.yaml")):
        try:
            spec = yaml.safe_load(yf.read_text(encoding="utf-8"))
            for tool in spec.get("tools", []):
                tool_name = tool.get("name", yf.stem)
                for arg, env_name in tool.get("secrets", {}).get("env", {}).items():
                    add(env_name,
                        "replace-me",
                        f"Required by tool '{tool_name}' — maps to parameter '{arg}'")
        except Exception as exc:
            print(f"  ⚠  Could not parse {yf.name}: {exc}", file=sys.stderr)
elif not yaml:
    print("  ⚠  pyyaml not installed — secret vars from YAML tools will be missing.", file=sys.stderr)

lines = []
for var_name, (default, comment) in entries.items():
    for c_line in comment.splitlines():
        lines.append(f"# {c_line.lstrip('# ')}")
    lines.append(f"{var_name}={default}")
    lines.append("")

out_path.write_text("\n".join(lines), encoding="utf-8")
print(f"  Created {out_path}")
PY

  printf '\n'
fi

# ─────────────────────────────────────────────────────────────────────────────
# Step 2 — Collect the full list of required vars
# ─────────────────────────────────────────────────────────────────────────────

VARS_TSV="$(python3 - "$ENV_EXAMPLE" "$TOOLS_CONFIG_DIR" "$ENV_FILE" <<'PY'
import sys, os
from pathlib import Path

env_example_path = Path(sys.argv[1])
tools_dir        = Path(sys.argv[2])
env_file         = Path(sys.argv[3])

try:
    import yaml
    has_yaml = True
except ImportError:
    has_yaml = False

SECRET_KEYWORDS = {"password", "token", "secret", "api_key", "apikey", "key"}
SKIP_PROMPT     = {"MCP_TOOL_CONFIG_DIR"}

def is_secret(name):
    n = name.lower()
    return any(k in n for k in SECRET_KEYWORDS)

stored = {}
if env_file.exists():
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            stored[k.strip()] = v.strip()

entries = {}

def add_entry(name, comment, default):
    if name not in entries:
        entries[name] = {
            "comment": comment,
            "default": default,
            "is_secret": is_secret(name),
            "skip": name in SKIP_PROMPT,
        }

def _is_decoration(text):
    """True for pure separator lines like '── MCP server ───────────────'."""
    stripped = text.replace("─", "").replace("-", "").replace("=", "").strip()
    return len(stripped) <= 20 and len(text) > 10

if env_example_path.exists():
    pending = []
    for raw in env_example_path.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip()
        if line.startswith("#"):
            text = line[1:].strip()
            if text and not _is_decoration(text):
                pending.append(text)
        elif "=" in line:
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip()
            if k:
                add_entry(k, " — ".join(pending) or k, v)
            pending = []
        elif not line:
            pending = []

if has_yaml and tools_dir.exists():
    for yf in sorted(tools_dir.glob("*.yaml")):
        try:
            spec = yaml.safe_load(yf.read_text(encoding="utf-8"))
            for tool in spec.get("tools", []):
                tool_name = tool.get("name", yf.stem)
                for arg, env_name in tool.get("secrets", {}).get("env", {}).items():
                    add_entry(env_name,
                              f"Required by tool '{tool_name}' (parameter: {arg})",
                              "replace-me")
        except Exception:
            pass

PLACEHOLDER = {"replace-me", "replace_me", ""}
for name, info in entries.items():
    if info["skip"]:
        continue
    current = stored.get(name, "")
    print("\t".join([
        name,
        info["comment"].replace("\t", " "),
        "1" if info["is_secret"] else "0",
        current,
        info["default"],
    ]))
PY
)"

# ─────────────────────────────────────────────────────────────────────────────
# Step 3 — Interactive prompts
# ─────────────────────────────────────────────────────────────────────────────

declare -A COLLECTED

printf '\n'
printf '─────────────────────────────────────────────────────\n'
printf ' Environment setup\n'
printf ' Non-secrets: press Enter to keep the value in [brackets]\n'
printf ' Secrets: press Enter to keep the existing value (hidden)\n'
printf '─────────────────────────────────────────────────────\n'

while IFS=$'\t' read -r VAR DESCRIPTION IS_SECRET CURRENT DEFAULT <&3; do
  printf '\n  \033[1m%s\033[0m\n' "$VAR"
  [[ -n "$DESCRIPTION" ]] && printf '  %s\n' "$DESCRIPTION"

  IS_PLACEHOLDER=0
  if [[ -z "$CURRENT" || "$CURRENT" == "replace-me" || "$CURRENT" == "replace_me" ]]; then
    IS_PLACEHOLDER=1
  fi

  if [[ "$IS_SECRET" == "1" ]]; then
    if [[ "$IS_PLACEHOLDER" == "0" ]]; then
      printf '  Value (hidden, press Enter to keep existing): '
      read -rs VALUE
      printf '\n'
      if [[ -z "$VALUE" ]]; then
        VALUE="$CURRENT"
      fi
    else
      printf '  Value (hidden, required): '
      VALUE=""
      while [[ -z "$VALUE" ]]; do
        read -rs VALUE
        printf '\n'
        if [[ -z "$VALUE" ]]; then
          printf '  ✗ Value cannot be empty. Try again: '
        fi
      done
    fi
  else
    if [[ "$IS_PLACEHOLDER" == "0" ]]; then
      printf '  Value [%s]: ' "$CURRENT"
      FALLBACK="$CURRENT"
    elif [[ -n "$DEFAULT" && "$DEFAULT" != "replace-me" && "$DEFAULT" != "replace_me" ]]; then
      printf '  Value [%s]: ' "$DEFAULT"
      FALLBACK="$DEFAULT"
    else
      printf '  Value: '
      FALLBACK=""
    fi
    read -r VALUE
    if [[ -z "$VALUE" ]]; then
      VALUE="$FALLBACK"
    fi
  fi

  COLLECTED["$VAR"]="$VALUE"
done 3<<< "$VARS_TSV"

# ─────────────────────────────────────────────────────────────────────────────
# Step 4 — Write .env
# ─────────────────────────────────────────────────────────────────────────────

TMP_PAIRS=$(mktemp)
trap 'rm -f "$TMP_PAIRS"' EXIT

for K in "${!COLLECTED[@]}"; do
  printf '%s\n' "${K}=${COLLECTED[$K]}"
done > "$TMP_PAIRS"

python3 - "$ENV_FILE" "$TMP_PAIRS" <<'PY'
import sys
from pathlib import Path

env_file   = Path(sys.argv[1])
pairs_file = Path(sys.argv[2])

new_values = {}
for raw in pairs_file.read_text(encoding="utf-8").splitlines():
    if "=" in raw:
        k, _, v = raw.partition("=")
        new_values[k] = v

existing_lines = []
if env_file.exists():
    existing_lines = env_file.read_text(encoding="utf-8").splitlines()

out_lines = []
updated = set()
for raw in existing_lines:
    line = raw.rstrip()
    if line and not line.startswith("#") and "=" in line:
        k = line.split("=", 1)[0].strip()
        if k in new_values:
            out_lines.append(f"{k}={new_values[k]}")
            updated.add(k)
        else:
            out_lines.append(line)
    else:
        out_lines.append(line)

new_keys = [k for k in new_values if k not in updated]
if new_keys:
    if out_lines and out_lines[-1] != "":
        out_lines.append("")
    for k in new_keys:
        out_lines.append(f"{k}={new_values[k]}")

all_keys = {l.split("=",1)[0] for l in out_lines if "=" in l and not l.startswith("#")}
if "MCP_TOOL_CONFIG_DIR" not in all_keys:
    out_lines.append("")
    out_lines.append("# Path to tool YAML directory (Docker: /app/tools)")
    out_lines.append("# Local: overridden by run_local.sh")
    out_lines.append("MCP_TOOL_CONFIG_DIR=/app/tools")

env_file.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
print(f"\n✓  .env written → {env_file}")
PY

rm -f "$TMP_PAIRS"
trap - EXIT

# ─────────────────────────────────────────────────────────────────────────────
# Step 5 — Source .env and apply local path overrides
# ─────────────────────────────────────────────────────────────────────────────

printf '\n'
info "Loading .env"
set -a
# shellcheck source=/dev/null
source "$ENV_FILE"
set +a

export MCP_TOOL_CONFIG_DIR="$ROOT_DIR/tools"   # local path always wins
export MCP_SERVER_NAME="${MCP_SERVER_NAME:-mcpproxy}"
export MCP_ENV_FILE="$ENV_FILE"
unset MCP_REPOS_DIR  # no longer used

# ─────────────────────────────────────────────────────────────────────────────
# Step 6 — Virtualenv + dependencies
# ─────────────────────────────────────────────────────────────────────────────

VENV_DIR="$ROOT_DIR/.venv"

if [[ ! -d "$VENV_DIR" ]]; then
  info "Creating virtual environment at .venv"
  python3 -m venv "$VENV_DIR"
fi

# shellcheck source=/dev/null
source "$VENV_DIR/bin/activate"

info "Installing / syncing dependencies"
pip install --quiet -r requirements.txt

# ─────────────────────────────────────────────────────────────────────────────
# Step 7 — Launch
# ─────────────────────────────────────────────────────────────────────────────

printf '\n'
ok "Starting MCP server"
printf '     Config dir : %s\n' "$MCP_TOOL_CONFIG_DIR"
printf '     Server name: %s\n' "$MCP_SERVER_NAME"
printf '     MCP        : http://0.0.0.0:8888/mcp\n'
printf '     UI         : http://0.0.0.0:8889\n'
printf '\n'

exec python server.py

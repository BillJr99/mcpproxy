# mcpproxy: Config-Driven MCP Host

> **⚠️ Disclaimer:** This software is **experimental** and provided **as-is**, with no
> guarantees of security, stability, or fitness for any particular purpose. It has not
> undergone a security audit. Do not expose it to untrusted networks or use it to handle
> sensitive data in production. See [LICENSE](LICENSE) for the full MIT license terms.

A Dockerized, config-driven MCP server with a built-in web UI.  
Each tool **provider** is a single YAML file under `tools/`. The YAML contains:

- The Python code for all tool functions (embedded directly in the file)
- One or more tool declarations that reference those functions
- Per-tool input schemas, secrets, and auth metadata
- Or a `package:` block to delegate to any existing MCP subprocess server — launched
  via `npx`, `uvx`, `python -m`, or any installed binary
- Or a `package:` + `repository:` pair to clone a git repo, run build commands, and
  spawn the resulting stdio MCP server — useful for servers distributed only as source
- Or a `package:` block running the [`mcp-remote`](https://www.npmjs.com/package/mcp-remote)
  bridge to reach a **remote, OAuth-protected** server (e.g. the official Asana MCP) — the
  bridge walks you through the OAuth flow and refreshes the token automatically. The web UI's
  **Remote MCP Server** wizard option builds this for you from just the server URL.

`server.py` loads every YAML at startup, installs declared `requirements` (pip packages),
runs `setup_commands`, then registers each tool automatically — no Python files to
maintain separately, no changes to `server.py` needed when adding new tools.

Two **built-in tools** (`mcpproxy__listfiles` and `mcpproxy__getfile`) are always registered
without any YAML config.  They give LLMs read-only access to a configurable directory
(default: `/app/files`, mountable as a Docker volume) — useful for retrieving screenshots
and snapshots produced by package providers such as Playwright MCP.

## Tool names advertised to the LLM

Every tool is advertised to MCP clients as **`<provider>__<tool>`** — the provider
name (the YAML filename without the `.yaml` extension, normalized to `[a-zA-Z0-9-]`)
joined to the tool's own `name` by a double underscore. For example, a YAML file
`tools/playwright.yaml` declaring a tool `browser_navigate` is exposed to the LLM
as `playwright__browser_navigate`. This guarantees that tools from different
providers cannot collide, even if they happen to share a name.

The two built-in tools follow the same convention: `mcpproxy__listfiles` and
`mcpproxy__getfile`. The `name:` field in your YAML stays unprefixed — the prefix
is added automatically when the tool is registered.

## Ports

| Port | Service |
|---|---|
| **8888** | MCP endpoint — `http://localhost:8888/mcp` |
| **8889** | Web UI & OpenAI-compatible tools endpoint — `http://localhost:8889` |

## Layout

```text
.
├── Dockerfile
├── docker-compose.yml              ← base: named volumes (prod/CI)
├── docker-compose.override.yml     ← dev: bind mounts (auto-merged locally)
├── run_local.sh                    ← interactive local setup + launch
├── requirements.txt
├── requirements-dev.txt            ← test dependencies
├── server.py
├── config.py                       ← shared env-var config (imported by all modules)
├── process_runner.py               ← spawns & proxies any stdio MCP subprocess
├── builtin_tools.py                ← built-in mcpproxy__listfiles / mcpproxy__getfile tools
├── frontend/
│   └── app.py                      ← FastAPI UI server (port 8889)
├── .env.example
├── handlers/
│   └── elicitation.py              ← shared mid-call input helper
├── tests/
│   ├── conftest.py
│   ├── test_server.py
│   ├── test_frontend.py
│   ├── test_with_ollama.sh         ← quick end-to-end MCP + Ollama sanity check
│   ├── mcp_interactive.sh          ← interactive tool picker & tester
│   └── ollama_agent.py             ← agentic tool-calling loop (Python)
└── tools/                          ← gitignored; mount at runtime
    └── <your-provider>.yaml        ← provider: code + tool declarations
```

`tools/` is gitignored — it is never committed and is mounted into the container at runtime.

## Web UI

Open **`http://localhost:8889`** in your browser after starting the server.

### Tools tab

- Browse all loaded providers (left panel)
- Click any provider to open its fields in a form editor
- Edit documentation, code, and per-tool fields (name, description, parameters)
- Add or remove tools with the **+ Add Tool** / **✕** buttons
- **Enable / disable** individual tools with the switch in each tool card's header.
  A disabled tool is kept in YAML (as `enabled: false`) but not registered with MCP
  and not shown to the LLM — toggle it back on later without re-typing the schema.
- **Function / Tool-name menu** — when mcpproxy can discover the legal set of names
  (`async def` symbols in your code, or `tools/list` returned by a package's
  stdio server), the field becomes a dropdown plus an **Other…** option so you can
  pick from the menu or type a custom value. Discovery runs automatically when you
  open a provider, when the code changes, and when the package command field loses
  focus; the **↻ Re-scan** button forces a refresh. Failure is silent — the
  dropdown just falls back to "Other…" so you can always free-type.
- **Save** — write the file; restart MCP server to reload
- **🔑 Secrets** — manage `.env` values for secrets declared in this provider
- **Delete** — remove the provider YAML

### New Provider wizard

Click **+ New Provider** and choose a provider type:

| Type | Description |
|---|---|
| **Python code** | Write `async def` functions; the UI lists the ones it finds as you type. Each becomes a tool entry. |
| **Package** | Enter any command that launches a stdio MCP server (`npx`, `uvx`, `python -m`, or an installed binary). When you click **Next**, mcpproxy auto-introspects the command and pre-populates the tool list; if introspection fails you can still proceed and add tools by hand. |
| **Repository** | Provide a git URL and a list of build commands. mcpproxy clones the repo, runs the build commands, then introspects the resulting stdio MCP server. The URL and build commands are persisted in YAML so the repo can be re-cloned and re-built automatically on every container restart. |

After the provider step, the wizard shows a **Secrets** step: any `secrets.env` entries
in the provider are listed, and you can fill in their values to save them directly to `.env`.

### Secrets manager

The **🔑 Secrets** button (also available in the wizard's final step) reads all `secrets.env`
entries from the selected provider, shows which variables are already set in `.env`, and lets
you fill in or update missing values — all without leaving the browser.

### Setup Commands

Each provider has a **Setup Commands** list (editable in the editor panel, saved to YAML).
These shell commands run automatically every time the MCP server starts — perfect for
installing browser binaries, downloading data, or any one-time setup that must survive a
Docker restart.

Example — for a Playwright package provider:
```
npx playwright install chrome
```

Commands run in order before the server accepts connections. The subprocess package is
launched lazily on the first tool call, not at startup, so the browser binary is always
ready when needed.

> **After editing and saving** a provider's command or setup steps, click **Restart MCP Server**
> (the yellow bar that appears after saving) to apply the changes.

## Secrets

Each tool provider YAML declares its required environment variables under `secrets.env`:

```yaml
tools:
  - name: my_tool
    ...
    secrets:
      env:
        api_key: MY_SERVICE_API_KEY   # handler arg → env var name
```

The server injects the value of `MY_SERVICE_API_KEY` from the environment at call time.
The LLM never sees the value — it is not in the tool schema.

**Ways to set secret values:**

1. **Web UI Secrets manager** — open `http://localhost:8889`, select a provider, click **🔑 Secrets**.
   Values are written to `.env` automatically.
2. **Wizard** — the final step of the **+ New Provider** wizard lists all required secrets and saves them to `.env`.
3. **Manually** — copy `.env.example` to `.env` and add your values:

```bash
cp .env.example .env
# Add entries like: MY_SERVICE_API_KEY=your-value-here
```

4. **run_local.sh** — prompts for all missing values and writes `.env`.

The `.env` file is consumed by Docker Compose via `env_file`. Credentials are never part of the MCP tool schema, so they are not exposed as LLM-visible tool arguments. Do not commit `.env`.

## Run locally

```bash
./run_local.sh
```

The script will:
1. Generate `.env.example` from the YAML tool files if it doesn't exist.
2. Prompt for any missing or placeholder values and write `.env`.
3. Override `MCP_TOOL_CONFIG_DIR` to the correct local path.
4. Create `.venv`, install dependencies, and start the server.

The Web UI and OpenAI-compatible tools endpoint are available at `http://localhost:8889`; the MCP endpoint is at `http://localhost:8888/mcp`.

## Run with Docker

### Pull and run the pre-built image from GHCR

Every push to `main` publishes a fresh image to the GitHub Container Registry.
You don't need to clone the repo or build anything.

```bash
docker pull ghcr.io/billjr99/mcpproxy:latest
```

**Minimum run command** — bind-mount your `tools/` directory and pass secrets via an env file.
`handlers/` is baked into the image; no mount needed.

```bash
docker run -d --rm \
  -p 8888:8888 -p 8889:8889 \
  --env-file .env \
  -v "$(pwd)/tools":/app/tools \
  --name mcpproxy \
  ghcr.io/billjr99/mcpproxy:latest
```

**Run with persistent caches and artefacts** — add named volumes so cloned repos,
package caches, and provider output files survive container restarts:

```bash
docker run -d --rm \
  -p 8888:8888 -p 8889:8889 \
  --env-file .env \
  -v "$(pwd)/tools":/app/tools \
  -v mcpproxy-files:/app/files \
  -v mcpproxy-repos:/app/repos \
  -v mcpproxy-cache:/root/.cache \
  -v mcpproxy-npm:/root/.npm \
  -v mcpproxy-uv-tools:/root/.local/share/uv \
  -v mcpproxy-mcp-auth:/app/.mcp-auth \
  -e MCP_REMOTE_CONFIG_DIR=/app/.mcp-auth \
  --name mcpproxy \
  ghcr.io/billjr99/mcpproxy:latest
```

The `mcpproxy-mcp-auth` volume holds the OAuth token cache for `mcp-remote` bridge
providers (e.g. the official Asana MCP). Persist it and you authorize once, then the
token refreshes silently; drop it and you re-authorize on every fresh container. Also
map the OAuth callback port (`-p 3334:3334`) the first time you authorize. Omit both if
you have no OAuth-bridge providers.

Every volume above is optional — omit any subset and that path falls back to the
container's ephemeral writable layer. See **[Volumes & caching](#volumes--caching)**
below for what each one covers and the cold-start speedup it provides.

MCP endpoint: **`http://localhost:8888/mcp`**  
Web UI & OpenAI-compatible tools endpoint: **`http://localhost:8889`**

The `-d` flag runs the container as a daemon and returns you to the shell immediately.
Follow logs with `docker logs -f mcpproxy`; stop the container with `docker stop mcpproxy`.

> **Note:** `tools/` is never baked into the image and must be supplied at runtime via a volume mount.
> `handlers/` is part of the image — no mount required.

**Run from a persistent home directory** — store tools and secrets in `~/.mcpproxy` so
you can run the image from any working directory and the web UI can read and write `.env`.
This is the recommended day-to-day command — it combines the persistent home directory
with the named cache volumes:

```bash
# First time only — create the directory and an empty .env
mkdir -p ~/.mcpproxy/tools
touch ~/.mcpproxy/.env

docker run -d \
  -p 8888:8888 -p 8889:8889 \
  --env-file "$HOME/.mcpproxy/.env" \
  -e MCP_ENV_FILE=/app/.env \
  -v "$HOME/.mcpproxy/tools:/app/tools" \
  -v "$HOME/.mcpproxy/.env:/app/.env" \
  -v mcpproxy-files:/app/files \
  -v mcpproxy-repos:/app/repos \
  -v mcpproxy-cache:/root/.cache \
  -v mcpproxy-npm:/root/.npm \
  -v mcpproxy-uv-tools:/root/.local/share/uv \
  -v mcpproxy-mcp-auth:/app/.mcp-auth \
  --name mcpproxy \
  ghcr.io/billjr99/mcpproxy:latest
```

#### `.env`: the two flags it needs, and why

The `.env` file is referenced **twice** above, and each reference does a different job —
both point at the **same local file** on your host:

| Flag | Local path → target | What it does |
| ---- | ------------------- | ------------ |
| `--env-file "$HOME/.mcpproxy/.env"` | host file, parsed by Docker | Reads the file and injects each `KEY=value` line as an **environment variable** in the container at startup. |
| `-v "$HOME/.mcpproxy/.env:/app/.env"` | host file → `/app/.env` | Bind-**mounts the file itself** into the container so the proxy can read it directly (via `MCP_ENV_FILE`, which the image defaults to `/app/.env`) and pass values to the MCP tool subprocesses it spawns. It also lets the web UI's **🔑 Secrets** panel read and write values live. |

Notes:
- In **both** flags, the path is your **local** `.env` on the host — `--env-file` takes the
  host path directly, and the **left** side of `-v host:container` is the host path while
  the **right** side (`/app/.env`) is where it appears inside the container.
- The file must already exist (hence the `touch` above). If it's missing, `--env-file`
  errors that the file isn't found, and the `-v` mount would create a *directory* named
  `.env` instead.
- Docker does **not** expand `~` inside double quotes, so use `$HOME` instead.
- `-e MCP_ENV_FILE=/app/.env` is shown for clarity, but it's optional — the image already
  defaults `MCP_ENV_FILE` to `/app/.env`. You only *need* it if you mount the file somewhere
  else.

The `mcpproxy-mcp-auth` volume holds the OAuth token cache for `mcp-remote` bridge
providers (e.g. the official Asana MCP); persist it and you authorize once. Map the OAuth
callback port (`-p 3334:3334`) the first time you authorize. Omit any volume you don't need
— each falls back to the container's ephemeral writable layer.

Available tags:

| Tag | When updated |
|---|---|
| `latest` | Every push to `main` |
| `main` | Every push to `main` |
| `vX.Y.Z` | On a version tag |
| `sha-<short>` | Per-commit SHA |

### Local development (bind mounts)

`docker-compose.override.yml` is merged automatically when you run `docker compose`
without a `-f` flag:

```bash
# First run: build and start
docker compose up --build

# Subsequent runs
docker compose up

# Run in the background
docker compose up -d

# Follow logs
docker compose logs -f

# Stop
docker compose down
```

Restart the container to pick up changes to tool YAML files:

```bash
docker compose restart mcp-host
```

Or use the **Restart MCP Server** button in the web UI.

### Production / CI (named volumes)

Populate the tools volume once before the first run:

```bash
docker run --rm \
  -v mcpproxy-tools:/dst \
  -v "$(pwd)/tools":/src:ro \
  alpine sh -c "cp -r /src/. /dst/"
```

Then start with only the base file:

```bash
docker compose -f docker-compose.yml up -d
```

### Environment variables and secrets

```bash
cp .env.example .env
# edit .env — set required values
```

Or use the web UI's Secrets manager at `http://localhost:8889`.

Docker Compose reads `.env` via `env_file:`. The file is never copied into the image. Do not commit `.env`.

### Custom ports

```bash
MCP_HOST_PORT=9000 UI_HOST_PORT=9001 docker compose up
```

### Volumes & caching

`docker-compose.yml` declares seven named volumes. Only the first is required —
the rest persist caches, artefacts, and OAuth tokens that would otherwise be
re-downloaded, re-built, or re-authorized on every fresh container.

| Container path | Volume | Holds | Without it (cold start) |
|---|---|---|---|
| `/app/tools` | `mcpproxy-tools` | Provider YAML configs | **Required** — the proxy has nothing to serve. |
| `/app/files` | `mcpproxy-files` | Provider output artefacts (Playwright screenshots, snapshots, …) surfaced via `mcpproxy__listfiles` / `mcpproxy__getfile` | Files vanish on container removal. |
| `/app/repos` | `mcpproxy-repos` | Cloned git workdirs + their build artefacts (`node_modules`, `dist`, …) for repository-mode providers | Re-clones and re-runs every `build_commands` on each start (seconds to several minutes per repo). |
| `/root/.cache` | `mcpproxy-cache` | XDG caches: pip wheels, uv wheels, Playwright browser binaries (`ms-playwright`) | pip/uvx re-download wheels; `npx playwright install chrome` re-fetches ~150 MB. |
| `/root/.npm` | `mcpproxy-npm` | npm/npx package cache | npx re-downloads packages from the npm registry on first call. |
| `/root/.local/share/uv` | `mcpproxy-uv-tools` | uvx per-tool venvs | uvx re-creates per-tool venvs from cached wheels. |
| `/app/.mcp-auth` | `mcpproxy-mcp-auth` | OAuth token cache (access + refresh tokens) for `mcp-remote` bridge providers, e.g. the official Asana MCP (`MCP_REMOTE_CONFIG_DIR`). Kept out of `/app/files` so tokens aren't exposed via `mcpproxy__getfile`. | Re-authorize through the browser on every fresh container. Only relevant if you run an OAuth-bridge provider. |

In dev (`docker-compose.override.yml`), `mcpproxy-tools`, `mcpproxy-files`,
`mcpproxy-repos`, and `mcpproxy-mcp-auth` are replaced with bind mounts (`./tools`,
`./files`, `./repos`, `./.mcp-auth`) so you can inspect or edit them from the host
(`./.mcp-auth` is gitignored — it holds live tokens). The three cache volumes remain
named volumes even in dev — they're opaque package-manager state, not files you read.

For ephemeral / CI runs, drop any subset of volumes — the proxy still works,
just slower on the first tool call after each cold start.

---

## Connecting AI clients to this MCP server

The MCP endpoint is `http://localhost:8888/mcp` (or replace `localhost` with your
Docker host IP / domain for remote access).

### Claude Code (Anthropic CLI)

Add the server as a named MCP entry using the HTTP transport:

```bash
claude mcp add --transport http mcpproxy http://localhost:8888/mcp
```

Or add it project-locally (stored in `.mcp.json` in the project root):

```bash
claude mcp add --transport http --scope project mcpproxy http://localhost:8888/mcp
```

Verify it is registered:

```bash
claude mcp list
```

Claude Code will now list and call your tools automatically during any chat session.

### Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json`
(macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "mcpproxy": {
      "url": "http://localhost:8888/mcp",
      "transport": "http"
    }
  }
}
```

Restart Claude Desktop — your tools appear in the tools panel.

### Cursor

Open **Cursor Settings → Features → MCP** and add a server entry:

```json
{
  "mcpServers": {
    "mcpproxy": {
      "url": "http://localhost:8888/mcp",
      "transport": "http"
    }
  }
}
```

### Cline (VS Code extension)

In VS Code, open the Cline sidebar → **MCP Servers** tab → **Add MCP Server**:

- **Transport**: `HTTP / SSE`
- **URL**: `http://localhost:8888/mcp`
- **Name**: `mcpproxy`

### Continue (VS Code / JetBrains extension)

Add to `.continue/config.json`:

```json
{
  "mcpServers": [
    {
      "name": "mcpproxy",
      "transport": {
        "type": "http",
        "url": "http://localhost:8888/mcp"
      }
    }
  ]
}
```

### OpenCode

Add to your `opencode.json` (or `~/.config/opencode/config.json`):

```json
{
  "mcp": {
    "servers": {
      "mcpproxy": {
        "url": "http://localhost:8888/mcp",
        "type": "remote"
      }
    }
  }
}
```

### Windsurf

Open **Windsurf Settings → Cascade → MCP** and add:

```json
{
  "mcpServers": {
    "mcpproxy": {
      "serverUrl": "http://localhost:8888/mcp"
    }
  }
}
```

### Zed

In `~/.config/zed/settings.json`:

```json
{
  "context_servers": {
    "mcpproxy": {
      "command": {
        "path": "npx",
        "args": ["-y", "@modelcontextprotocol/server-fetch"],
        "env": {}
      }
    }
  }
}
```

> **Note:** Zed currently supports stdio-based MCP servers natively. For HTTP-transport
> servers, use an MCP-to-stdio bridge such as `mcp-remote`:
> ```bash
> npx -y mcp-remote http://localhost:8888/mcp
> ```
> Then point Zed at that bridge command.

### Ollama (tool-calling models)

Ollama itself does not speak MCP — use the included `tests/ollama_agent.py` script,
which bridges MCP → Ollama tool-calling automatically:

```bash
python3 tests/ollama_agent.py "List the tools you have available"
```

The script queries `http://localhost:11434/api/tags` for available models, shows a
numbered selection menu, then drives a full agentic tool-calling loop.

Override defaults with environment variables:

```bash
OLLAMA_BASE=http://mymachine:11434 \
OLLAMA_MODEL=qwen3:14b \
MCP_BASE=http://localhost:8888/mcp \
python3 tests/ollama_agent.py "Do something useful"
```

### Models without native MCP support (Pi, Hermes, GPT-4o, etc.)

For any model that does not support MCP natively, you can describe the available tools
in the system prompt or at the start of a conversation. List the MCP endpoint and paste
in the JSON schema from `tools/list`:

```bash
# Fetch the tool schemas
curl -s http://localhost:8888/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' \
  | python3 -m json.tool
```

Example system prompt snippet:

```
You have access to the following tools via an MCP server at http://localhost:8888/mcp.
To call a tool, output a JSON block with the tool name and arguments; I will execute
the call and paste the result back.

Tools:
<paste tools/list output here>
```

Then manually relay tool calls and results between the model and the MCP server during
the conversation.

---

## Test scripts

### `tests/test_with_ollama.sh` — quick sanity check

Runs MCP initialize → tools/list (and optionally tools/call) and asks Ollama to
summarise the results.

```bash
bash tests/test_with_ollama.sh

# Override defaults
MCP_URL=http://localhost:8888/mcp \
OLLAMA_MODEL=qwen3:14b \
RUN_REAL_TOOL=1 \
bash tests/test_with_ollama.sh
```

### `tests/mcp_interactive.sh` — interactive tool tester

Pick any registered tool, get prompted for parameters, call the tool, and get an Ollama
summary of the result. Secrets are checked for presence only — values are never printed.

```bash
bash tests/mcp_interactive.sh

# Override defaults
MCP_URL=http://localhost:8888/mcp \
UI_URL=http://localhost:8889 \
OLLAMA_URL=http://localhost:11434 \
bash tests/mcp_interactive.sh
```

### `tests/ollama_agent.py` — agentic loop

Drives a full agentic tool-calling loop: MCP initialize → tools/list → Ollama chat with
tool schemas → execute tool_calls → feed results back → repeat until a final text answer.

```bash
python3 tests/ollama_agent.py "Go to https://example.com and summarise the page"

# Override defaults
OLLAMA_BASE=http://localhost:11434 \
OLLAMA_MODEL=llama3.2 \
MCP_BASE=http://localhost:8888/mcp \
python3 tests/ollama_agent.py "What tools do you have?"
```

---

## Running unit tests

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest tests/ -v
```

Tests cover `server.py` (pure helpers), `frontend/app.py` (all API endpoints), and
`builtin_tools.py` (file listing and retrieval).
CI runs on every push via `.github/workflows/tests.yml`.

---

## Security notes

- Do not commit `.env`.
- Do not enable `debug: true` outside of local testing.
- The web UI has no authentication — run it on a trusted network only.

---

## Tutorial: adding a new tool

Every provider is a single YAML file under `tools/`.

### Part 1 — a simple tool with no secrets

#### Step 1 — create `tools/ping.yaml`

```yaml
code: |
  import datetime
  from typing import Any

  async def ping(context: dict[str, Any], message: str = "hello") -> dict[str, Any]:
      return {
          "ok": True,
          "echo": message,
          "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
      }

tools:
  - name: ping
    function: ping
    description: Echo a message back with a server-side UTC timestamp.
    input_schema:
      type: object
      properties:
        message:
          type: string
          default: "hello"
          description: The text to echo back.
      required: []
```

#### Step 2 — restart and test

```bash
./run_local.sh
```

```bash
# The provider file is tools/ping.yaml, so the advertised tool name is "ping__ping".
curl -s -X POST http://localhost:8888/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"ping__ping","arguments":{"message":"world"}}}'
```

---

### Part 2 — a tool with injected secrets

```yaml
code: |
  import urllib.request, json, traceback
  from typing import Any

  async def get_weather(context, latitude, longitude, api_key):
      try:
          url = f"https://api.open-meteo.com/v1/forecast?latitude={latitude}&longitude={longitude}&current_weather=true"
          with urllib.request.urlopen(url, timeout=10) as r:
              data = json.loads(r.read())
          return {"ok": True, **data.get("current_weather", {})}
      except Exception as e:
          traceback.print_exc()
          return {"ok": False, "error": str(e)}

tools:
  - name: get_weather
    function: get_weather
    description: Return current weather at a coordinate.
    input_schema:
      type: object
      properties:
        latitude:
          type: number
        longitude:
          type: number
      required: [latitude, longitude]
    secrets:
      env:
        api_key: WEATHER_API_KEY
```

Add `WEATHER_API_KEY=replace-me` to `.env.example` and `.env` (or use the Secrets manager in the UI).

---

### Part 3 — a package provider (no code required)

Use the **+ New Provider → Package** wizard in the web UI, or create the YAML manually.
Any command that spawns a stdio MCP server works — `npx`, `uvx`, `python -m`, or an
installed binary:

```yaml
# ── npx (Node.js, no install needed) ─────────────────────────────────────────
package:
  command: npx @playwright/mcp@latest --headless --isolated --output-dir /app/files/playwright

setup_commands:
  - npx playwright install chrome   # installs browser on every startup
                                    # (cached in /root/.cache/ms-playwright via the
                                    #  mcpproxy-cache volume — only re-downloads on
                                    #  a fresh, unmounted container)

tools:
  # Populated automatically when the wizard introspects the command — or fill manually
  - name: browser_navigate                # advertised as playwright__browser_navigate
    description: Navigate to a URL in a browser.
    input_schema:
      type: object
      properties:
        url:
          type: string
          description: The URL to navigate to.
      required: [url]
```

```yaml
# ── uvx (Python package, no install needed) ───────────────────────────────────
package:
  command: uvx mcp-server-fetch

tools: []   # auto-populated by the wizard's introspection step
```

```yaml
# ── pip-installed Python module ───────────────────────────────────────────────
package:
  command: python -m mcp_server_github

requirements:
  - mcp-server-github   # installed by pip before the server starts

tools: []
```

```yaml
# ── globally installed npm binary ─────────────────────────────────────────────
package:
  command: mcp-server-github

setup_commands:
  - npm install -g @modelcontextprotocol/server-github

tools: []
```

> **`--headless`** runs Chromium without a visible window — required inside Docker or any
> headless server environment. Remove it if you want to watch the browser on a desktop.
> **`--isolated`** gives each session its own browser context (no shared cookies/storage).

The server spawns the process, performs the MCP handshake once, then forwards every tool
call to it. The process is reused across calls (started lazily on the first tool call).

---

### Part 3.25 — a remote, OAuth-protected server (e.g. the official Asana MCP)

Some MCP servers aren't stdio packages at all — they're **remote, OAuth-protected HTTP
endpoints**. The official Asana server is one: it lives at `https://mcp.asana.com/v2/mcp`
(Streamable HTTP) and is reached through an OAuth 2.1 authorization-code (PKCE) flow — there's
no static API key. mcpproxy speaks stdio to its upstreams, so these are bridged with the
community [`mcp-remote`](https://www.npmjs.com/package/mcp-remote) adapter, which is itself just
a `package:` command.

The easiest way to add one is the web UI's **+ New Provider → 🌐 Remote MCP Server** option:
paste the server URL (e.g. `https://mcp.asana.com/v2/mcp`) and mcpproxy builds the
`npx -y mcp-remote <url>` command, introspects the tool list, and walks you through the OAuth
flow. The equivalent YAML it produces (which you can also write by hand) is:

```yaml
# Paste into your tools/ config dir, or use the wizard's "Remote MCP Server"
# option (just paste the URL — it builds this command for you).
package:
  command: npx -y mcp-remote https://mcp.asana.com/v2/mcp

tools:
  - name: get_me      # advertised as asana__get_me; the rest are auto-introspected
    description: Return the Asana user that the authorized token belongs to.
    input_schema: { type: object, properties: {} }
```

**`mcp-remote` performs the OAuth walkthrough and refreshes the token for you** — mcpproxy
itself stays a thin stdio proxy:

- **First run** (or after the refresh token expires / is revoked): `mcp-remote` prints an
  authorization URL and blocks the MCP handshake until you authorize. When you introspect the
  server in the **+ New Provider → Remote MCP Server** (or **Package**) wizard, mcpproxy scrapes
  that URL from stderr and shows a clickable **🔐 Authorize** link (it's also logged as
  `authorization required … visit:`). Open it, approve access in Asana, and the localhost
  callback (`:3334`) completes the flow — introspection then continues automatically and the
  tool list populates.
- **Afterwards** the OAuth token cache is written under `MCP_REMOTE_CONFIG_DIR` and **the access
  token is refreshed silently** on every expiry. You don't authorize again until the refresh
  token itself lapses.

#### Persisting the token cache (so you authorize once)

`docker-compose.yml` wires this up: it sets `MCP_REMOTE_CONFIG_DIR=/app/.mcp-auth`, mounts the
`mcpproxy-mcp-auth` volume there (kept **out** of `/app/files` so tokens are never exposed via
`mcpproxy__getfile`), and maps the OAuth callback port `3334`. Keep that volume and the refresh
token survives restarts. In dev, `docker-compose.override.yml` bind-mounts `./.mcp-auth`
(gitignored).

#### Headless / one-time bootstrap

The OAuth redirect targets `localhost:3334`. Either authorize via the wizard link with port
`3334` mapped (the default), **or** run the flow once on the host to pre-populate the cache,
then start the proxy with the same dir mounted:

```bash
MCP_REMOTE_CONFIG_DIR=./.mcp-auth npx -y mcp-remote https://mcp.asana.com/v2/mcp
# authorize in the browser, then `docker compose up` — tools work with no further prompts
```

> **Pin the bridge** for reproducible builds once you've settled on a version, e.g.
> `npx -y mcp-remote@<version> …`. Add `--debug` to write a detailed auth/refresh log under
> `MCP_REMOTE_CONFIG_DIR`.

---

### Part 3.5 — a repository provider (clone + build + introspect)

For MCP servers that are published only as source code (no `npx` / `uvx` / pip distribution),
use a **repository provider**. mcpproxy will:

1. `git clone` the repo into a workdir under `MCPPROXY_REPOS_DIR` (default `/app/repos/<provider>`).
2. Run each entry of `build_commands` inside that workdir (e.g. `npm install`, `npm run build`).
3. Spawn the `package.command` from inside the workdir and introspect tools the same way as a
   package provider.
4. Re-run steps 1–3 on every server start so ephemeral containers always have a fresh build.

#### Adding one via the wizard

1. Click **+ New Provider** → choose **📂 Repository**.
2. Fill in:
   - **Provider name** — e.g. `linkedin`.
   - **Git URL** — `https://github.com/felipfr/linkedin-mcpserver` (https or ssh).
   - **Ref** *(optional)* — branch, tag, or commit SHA. Defaults to the repo's default branch.
   - **Build commands** — one per row. For most Node/TypeScript MCP repos: `npm install`, then `npm run build`. Click **⚡ Pre-fill Node/TS** to drop these in automatically along with the spawn command.
   - **Spawn command** — the stdio MCP launch command. For the compiled-TS pattern above, use `node build/index.js` (the `npm run build` step compiles `src/*.ts` → `build/*.js`). Runs inside the workdir.
3. Click **Next** — mcpproxy clones, builds, and introspects. The tool list is auto-populated.

> **Recommended for Node/TypeScript repos** (covers `linkedin-mcpserver` and most fastmcp-style projects):
>
> | Field | Value |
> |---|---|
> | Build commands | `npm install`<br>`npm run build` |
> | Spawn command | `node build/index.js` |
>
> The **⚡ Pre-fill Node/TS** button in the wizard's Build commands header populates all three at once.

> **Do not** put `npm run start:dev`, `npm start`, or any other long-running server command in **Build commands** — those go in **Spawn command**. Build commands must terminate; mcpproxy enforces a `MCPPROXY_BUILD_TIMEOUT` (default 600s) and aborts a hanging build.

#### YAML produced

```yaml
package:
  command: node build/index.js        # spawn command, run inside the workdir
repository:
  url: https://github.com/felipfr/linkedin-mcpserver
  ref: main                           # optional
  workdir: /app/repos/linkedin        # optional — defaults to <REPOS_DIR>/<provider>
  build_commands:
    - npm install
    - npm run build
  env_keys:                            # auto-discovered from .env.example
    - LINKEDIN_CLIENT_ID               # values live in MCP_ENV_FILE
    - LINKEDIN_CLIENT_SECRET           # (the proxy's .env) and are written
                                       # into <workdir>/.env on every build / spawn
tools:
  - name: search_jobs                 # advertised as linkedin__search_jobs
    description: Search LinkedIn job postings.
    input_schema:
      type: object
      properties:
        query: {type: string, description: "Search query"}
      required: [query]
```

The `package.command` is what spawns the MCP server (just like a regular package provider).
The new `repository:` block tells the server **how to materialize the workdir on startup**.

#### Secrets from `.env.example`

If the cloned repo contains a `.env.example` (or `.env.sample` / `.env.template`)
at its root, mcpproxy parses it after the clone step and surfaces every
`KEY=` line in two places:

1. The wizard's **Secrets** step (so you can fill in values immediately).
2. The provider's `repository.env_keys` list in YAML (editable in the
   **📂 Repository** editor box).

Values themselves live in `MCP_ENV_FILE` (the proxy's `.env`) — the same
storage every other secret uses. At spawn time and on every restart, the
server:

1. Reads the current values from `MCP_ENV_FILE` and the process environment.
2. Writes a `.env` file inside `<workdir>` containing only the keys that
   are actually set (empty / unset keys are skipped).
3. Passes the same values as environment variables to the spawned MCP
   subprocess.

This covers both server styles: code that calls `dotenv.config()` /
`tsx --env-file=.env` reads the on-disk file, while code that reads
`process.env.X` / `os.environ[X]` sees the env vars directly.

#### Build failures while secrets are missing

A common failure mode: a build command like `npm install` triggers a
`postinstall` script that requires secrets, but the user hasn't filled
them in yet. mcpproxy's wizard handles this gracefully:

- The clone step runs first, then `.env.example` is parsed.
- If a build command then fails, the wizard surfaces the error inline
  and **still** continues to the Secrets step with the discovered keys.
- After you fill in the secrets and save, `materialize_repository`
  re-runs the build on the next server start — with `<workdir>/.env`
  now populated — and the build succeeds.

#### Editing a repository provider

The editor shows a **📂 Repository** box with the git URL, ref, build
commands, and the auto-discovered env keys list.
- **↻ Re-clone & build** — re-runs `git pull` (or `git clone` on a fresh
  container) and the build commands, then re-introspects the spawn
  command. Newly-discovered env keys are merged into the list.
- **↻ Re-scan** on the env keys row — re-parses `.env.example` without
  re-running the build (useful if you've just pulled a new commit
  that adds variables).
- After saving, click **Restart MCP Server** to apply changes — on
  startup the server walks every repository provider, re-clones / pulls
  / re-builds, writes `<workdir>/.env`, then registers tools.

#### Environment variables

| Variable | Default | Description |
|---|---|---|
| `MCPPROXY_REPOS_DIR` | `/app/repos` | Base directory for cloned repos. |

The default `docker-compose.yml` mounts the `mcpproxy-repos` named volume here
(or `./repos` in dev via the override file) so cloned trees and their build
artefacts (`node_modules`, `dist`, …) survive container restarts. See
[Volumes & caching](#volumes--caching) for the full list.

Drop the volume entry for ephemeral / disposable containers — every container
start will re-clone and re-build into the container's writable layer.

#### Lifecycle on container restart

On every server start, `server.py` walks each YAML provider and:
- If the spec has a `repository:` block, runs `git clone` (or `git pull` if the workdir
  already contains `.git`), then re-runs every entry in `build_commands` with
  `cwd=<workdir>`.
- Then runs the standard `requirements:` (pip) and `setup_commands:` lists.
- Then registers the tools and spawns the MCP subprocess (lazily, on first tool call).

#### Security notes

- Build commands run as the server user with full shell-style splitting via `shlex.split`.
  Do **not** paste untrusted commands.
- The git URL is passed directly to `git clone`. Private repos require SSH keys or a
  credential helper to be configured inside the container.

#### Troubleshooting

| Symptom | What to check |
|---|---|
| Clone hangs or fails | The container must have outbound HTTPS / SSH to the git host. For SSH, mount your `~/.ssh` and configure `known_hosts`. |
| `npm install` / build fails | View container stdout: `docker compose logs -f`. All build output is streamed unbuffered. |
| Spawn / introspect fails | The repo must produce a working stdio MCP server. Check the spawn command resolves inside the workdir (e.g. `dist/main.js` only exists after a successful build). |
| Tools not appearing after edit | Click **Restart MCP Server** so the YAML is re-loaded and the workdir re-materialized. |

---

### pip Requirements vs setup_commands

| Feature | Use for |
|---|---|
| `requirements:` | pip packages to install in the Python environment (`httpx`, `requests`, etc.) |
| `setup_commands:` | Any other one-time setup — browser binaries, npm installs, data downloads |

Both run on every server startup (pip is a no-op if the package is already installed).

---

### Part 4 — multiple tools in one provider

A single YAML file can declare any number of tools sharing the same `code` block.

---

### Part 5 — error handling

Return `{"ok": True, ...}` on success, `{"ok": False, "error": "..."}` on failure. Never let an exception propagate — wrap the entire function body in `try/except`.

---

### Part 6 — calling blocking libraries with `asyncio.to_thread`

Handler functions are `async`, but many Python libraries block the event loop. Use `asyncio.to_thread()` to run them safely in a thread pool.

```python
result = await asyncio.to_thread(_fetch_sync, arg1, arg2)
```

---

### Part 7 — prompting the user mid-call (elicitation)

```python
from handlers.elicitation import request_text_input_with_fallback

sms_result = await request_text_input_with_fallback(
    context=context,
    field_name="sms_code",
    message="We sent an SMS to your phone.",
    description="Enter the six-digit code.",
)
```

---

### Part 8 — persisting state between calls

Write state to a well-known file path and read it on the next call.

---

### Part 9 — reading files produced by package providers

Package providers (e.g. Playwright MCP) often write files to disk — screenshots (PNG),
accessibility snapshots (JSON), downloaded pages (HTML) — that the LLM would otherwise
have no way to retrieve.

mcpproxy ships two **built-in utility tools** that are always registered, with no YAML
config file required:

| Tool | Description |
|---|---|
| `mcpproxy__listfiles` | List files and subdirectories inside the files base directory |
| `mcpproxy__getfile` | Read a file from the files base directory (UTF-8 text or base64) |

**Default base directory:** `/app/files` inside Docker (mounted as the
`mcpproxy-files` named volume, or `./files` in dev — see
[Volumes & caching](#volumes--caching)). Override with the `MCPPROXY_FILES_DIR`
environment variable. `run_local.sh` automatically sets it to `./files` under the
repo root when running outside Docker.

Each package provider should write its artefacts under its own subdirectory of
the base — e.g. Playwright is launched with
`npx @playwright/mcp@latest … --output-dir /app/files/playwright` so screenshots
land at `/app/files/playwright/screenshot.png`.

> **Note (migrating from earlier versions):** the default was previously
> `.playwright-mcp` (relative to the cwd, i.e. `/app/.playwright-mcp` inside
> Docker). If you have a custom `tools/playwright.yaml`, either add the
> `--output-dir /app/files/playwright` flag to its spawn command, or set
> `MCPPROXY_FILES_DIR=/app/.playwright-mcp` to keep the old layout.

Only files **inside** the base directory are accessible — path-traversal attempts
(`../`) are rejected.

#### Example workflow with Playwright

1. Ask the LLM to navigate to a page and take a screenshot via the Playwright MCP provider.
2. Playwright writes `screenshot.png` to `/app/files/playwright/` (because its spawn
   command includes `--output-dir /app/files/playwright`).
3. Ask the LLM to call `mcpproxy__listfiles` with `path="playwright"` — it returns the file list.
4. Ask the LLM to call `mcpproxy__getfile` with `path="playwright/screenshot.png"` — it returns
   the PNG as a base64 string that the LLM can describe or pass to a vision model.

#### `mcpproxy__listfiles` parameters

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `path` | string | No | `""` | Subdirectory to list, relative to the base dir. Omit to list the root. |

Returns an object with `ok`, `base_dir`, `path`, and `entries` (list of `{name, type, size}`).

#### `mcpproxy__getfile` parameters

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `path` | string | **Yes** | — | File path, relative to the base dir. |
| `encoding` | string | No | `"auto"` | `"auto"` tries UTF-8, falls back to base64. `"text"` forces UTF-8. `"base64"` always base64. |

Returns an object with `ok`, `path`, `size`, `content`, and `encoding`.

#### Changing the base directory

```bash
# In docker-compose.override.yml or as -e flag
MCPPROXY_FILES_DIR=/app/data
```

Or mount a different volume / host directory at the target path:

```yaml
volumes:
  - ./playwright-output:/app/files   # bind-mount host dir at the default location
```

By default `docker-compose.yml` mounts the named volume `mcpproxy-files` at
`/app/files`, and `docker-compose.override.yml` swaps that for `./files` in dev.

---

### YAML provider reference

```yaml
documentation: |                   # optional — shown in the web UI; markdown friendly
  Describe what this provider does, its tools, secrets, and any usage notes.

# ── Python code provider ──────────────────────────────────────────────────────

code: |                            # Python source — executed once at startup
  # Import anything, define helpers and async tool functions.

# ── Package provider (mutually exclusive with code) ───────────────────────────
# Supports any command: npx, uvx, python -m, or an installed binary.

package:
  command: string                  # e.g. "npx @playwright/mcp@latest --isolated --output-dir /app/files/playwright"
                                   #      "uvx mcp-server-fetch"
                                   #      "python -m mcp_server_github"
                                   #      "mcp-server-github"

# ── Repository provider (clone + build, spawned from inside the workdir) ──────
# When `repository:` is present, the `package.command` above is run with cwd
# set to the cloned workdir.  Clone + build re-runs on every server start.

repository:
  url: string                      # e.g. "https://github.com/owner/repo"
  ref: string                      # optional — branch, tag, or commit SHA
  workdir: string                  # optional — defaults to <MCPPROXY_REPOS_DIR>/<provider>
  build_commands:                  # shell commands run in <workdir> before spawn
    - npm install
    - npm run build
  env_keys:                        # optional — KEY names whose values live
    - MY_API_KEY                   # in MCP_ENV_FILE.  A .env file is written
    - SECRET_TOKEN                 # into <workdir> before every build / spawn.
                                   # Auto-discovered from .env.example.

# ── Shared optional fields (all provider types) ───────────────────────────────

requirements:                      # pip packages installed before the server starts
  - package-name
  - package-name==1.2.3

setup_commands:                    # shell commands run on every server startup
  - npx playwright install chrome  # (e.g. browser binaries, npm global installs)
  - echo "server ready"

# ── Tool declarations (required) ──────────────────────────────────────────────

tools:
  - name: string                   # tool name as written here; the LLM sees
                                   # "<provider>__<name>" (e.g. playwright__browser_navigate)
    function: string               # async function name from code block (code providers only)
    description: string            # shown to the LLM
    enabled: true                  # optional (default true); set false to keep the tool
                                   # in YAML but not advertise / register it
    documentation: string          # optional per-tool notes shown in the web UI
    input_schema:                  # JSON Schema
      type: object
      properties:
        arg_name:
          type: string|number|integer|boolean|array|object
          description: string
          default: any
      required: [arg_name]
    secrets:
      env:                         # optional
        handler_arg: ENV_VAR_NAME
    auth:                          # optional — forwarded to context["auth"]
      any_key: any_value
```


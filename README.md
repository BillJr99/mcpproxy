# mcpproxy: Config-Driven MCP Host

A Dockerized, config-driven MCP server with a built-in web UI.  
Each tool **provider** is a single YAML file under `tools/`. The YAML contains:

- The Python code for all tool functions (embedded directly in the file)
- One or more tool declarations that reference those functions
- Per-tool input schemas, secrets, and auth metadata
- Or an `npx:` block to delegate to an existing MCP npm package

`server.py` loads every YAML at startup, executes its `code` block (or spawns the
declared `npx` process), and registers each declared tool automatically — no Python
files to maintain separately, no changes to `server.py` needed when adding new tools.

## Ports

| Port | Service |
|---|---|
| **8888** | MCP endpoint — `http://localhost:8888/mcp` |
| **8889** | Web UI — `http://localhost:8889` |

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
├── npx_runner.py                   ← spawns & proxies npx-based MCP providers
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
- **Save** — write the file; restart MCP server to reload
- **🔑 Secrets** — manage `.env` values for secrets declared in this provider
- **Delete** — remove the provider YAML

### New Provider wizard

Click **+ New Provider** and choose a provider type:

| Type | Description |
|---|---|
| **Python code** | Write `async def` functions; declare tools that reference them |
| **npx package** | Enter an `npx` command (e.g. `npx @playwright/mcp@latest`); the UI auto-introspects the MCP server and populates tool definitions — no code needed |

After the provider step, the wizard shows a **Secrets** step: any `secrets.env` entries
in the provider are listed, and you can fill in their values to save them directly to `.env`.

### Secrets manager

The **🔑 Secrets** button (also available in the wizard's final step) reads all `secrets.env`
entries from the selected provider, shows which variables are already set in `.env`, and lets
you fill in or update missing values — all without leaving the browser.

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

The UI is available at `http://localhost:8889` and the MCP endpoint at `http://localhost:8888/mcp`.

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
docker run --rm \
  -p 8888:8888 -p 8889:8889 \
  --env-file .env \
  -v "$(pwd)/tools":/app/tools \
  ghcr.io/billjr99/mcpproxy:latest
```

MCP endpoint: **`http://localhost:8888/mcp`**  
Web UI: **`http://localhost:8889`**

> **Note:** `tools/` is never baked into the image and must be supplied at runtime via a volume mount.
> `handlers/` is part of the image — no mount required.

**Run from a persistent home directory** — store tools and secrets in `~/.mcpproxy` so
you can run the image from any working directory and the web UI can read and write `.env`:

```bash
# First time only — create the directory and an empty .env
mkdir -p ~/.mcpproxy/tools
touch ~/.mcpproxy/.env

docker run --rm \
  -p 8888:8888 -p 8889:8889 \
  --env-file "$HOME/.mcpproxy/.env" \
  -e MCP_ENV_FILE=/app/.env \
  -v "$HOME/.mcpproxy/tools:/app/tools" \
  -v "$HOME/.mcpproxy/.env:/app/.env" \
  ghcr.io/billjr99/mcpproxy:latest
```

Two things differ from the minimal command:
- `--env-file` injects secrets as environment variables at startup; Docker does **not** expand `~` inside double quotes, so `$HOME` is used instead.
- `-v "$HOME/.mcpproxy/.env:/app/.env"` + `-e MCP_ENV_FILE=/app/.env` also mount the file itself into the container so the web UI's **🔑 Secrets** panel can read and write values without leaving the browser.

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

Tests cover `server.py` (pure helpers) and `frontend/app.py` (all API endpoints).
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
curl -s -X POST http://localhost:8888/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"ping","arguments":{"message":"world"}}}'
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

### Part 3 — an npx-based provider (no code required)

Use the **+ New Provider → npx package** wizard in the web UI, or create the YAML manually:

```yaml
npx:
  command: npx @playwright/mcp@latest --isolated

tools:
  # Populated automatically by the UI's Introspect button — or fill manually
  - name: browser_navigate
    description: Navigate to a URL in a browser.
    input_schema:
      type: object
      properties:
        url:
          type: string
          description: The URL to navigate to.
      required: [url]
```

The server spawns the `npx` process, performs the MCP handshake once, then forwards
every tool call to it. The process is reused across calls.

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

### YAML provider reference

```yaml
documentation: |                   # optional — shown in the web UI; markdown friendly
  Describe what this provider does, its tools, secrets, and any usage notes.

# ── Python code provider ──────────────────────────────────────────────────────

code: |                            # Python source — executed once at startup
  # Import anything, define helpers and async tool functions.

# ── npx provider (mutually exclusive with code) ───────────────────────────────

npx:
  command: string                  # e.g. "npx @playwright/mcp@latest --isolated"

# ── Tool declarations (required) ──────────────────────────────────────────────

tools:
  - name: string                   # unique MCP tool name
    function: string               # async function name from code block (code providers only)
    description: string            # shown to the LLM
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

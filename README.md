# mcpproxy: Config-Driven MCP Host

This project is a Dockerized, config-driven MCP server with a built-in web UI.
Each tool **provider** is a single YAML file under `tools/`. The YAML contains:

- The Python code for all tool functions (embedded directly in the file)
- One or more tool declarations that reference those functions
- Per-tool input schemas, secrets, and auth metadata
- An optional `repo:` block to pull an external Git repo at startup

`server.py` loads every YAML at startup, executes its `code` block, and registers each
declared tool automatically — no Python files to maintain separately, no changes to
`server.py` needed when adding new tools.

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
├── repo_loader.py                  ← git clone/pull for repo: YAML blocks
├── frontend/
│   └── app.py                      ← FastAPI UI server (port 8889)
├── .env.example
├── handlers/
│   └── elicitation.py              ← shared mid-call input helper
├── tests/
│   ├── conftest.py
│   ├── test_server.py
│   ├── test_frontend.py
│   └── test_repo_loader.py
└── tools/                          ← gitignored; mount at runtime
    └── <your-provider>.yaml        ← provider: code + tool declarations
```

`tools/` and `repos/` are gitignored — they are never committed and are mounted into
the container at runtime.

## Web UI

Open **`http://localhost:8889`** in your browser after starting the server.

### Tools tab

- Browse all loaded providers (left panel)
- Click any provider to open its YAML in a CodeMirror editor
- **Validate** — check YAML structure before saving
- **Save** — write the file; restart MCP server to reload
- **🔑 Secrets** — manage `.env` values for secrets declared in this provider
- **Delete** — remove the provider YAML

### New Provider wizard

Click **+ New Provider** to open a three-step wizard:

| Step | Description |
|---|---|
| **Blank template** | Starts with a documented skeleton |
| **From Python code** | Paste existing `async def` functions; YAML is generated automatically from their signatures |
| **From Git repo** | Enter a repository URL; a `repo:` YAML block is generated and the server clones the repo at startup |

After the YAML step, the wizard shows a **Secrets** step: any `secrets.env` entries in the
provider are listed, and you can fill in their values to save them directly to `.env`.

### Discover tab

Browse a curated list of importable MCP servers (Filesystem, GitHub, Brave Search, Fetch, SQLite,
Postgres, and more). Click **Import** to pre-fill the repo wizard, or **Browse** to open the
server's source on GitHub. Links to `awesome-mcp-servers`, `mcpservers.org`, and
`modelcontextprotocol/servers` are also included.

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

### Port

```bash
MCP_HOST_PORT=9000 UI_HOST_PORT=9001 docker compose up
```

## External repo support (`repo:` block)

A provider YAML can include a `repo:` block to reference an external Git repository.
At startup the server clones the repo (or pulls if already cloned) and adds its root
(or a specified subfolder) to `sys.path`, making it importable from the `code:` block.

```yaml
repo:
  url: https://github.com/user/some-mcp-server   # required
  branch: main                                    # optional (default: main)
  subfolder: src                                  # optional sub-path added to sys.path
  path: repos/my-override                         # optional clone destination override
  requirements: requirements.txt                  # optional pip requirements file
  install_packages:                               # optional explicit pip packages
    - requests
    - some-library

code: |
  # The repo root (or subfolder) is on sys.path — import directly.
  from your_module import some_function
  from typing import Any

  async def my_tool(context: dict[str, Any], query: str) -> dict[str, Any]:
      return {"ok": True, "result": some_function(query)}

tools:
  - name: my_tool
    function: my_tool
    description: Calls a function from the external repo.
    input_schema:
      type: object
      properties:
        query:
          type: string
          description: Query string.
      required:
        - query
```

Cloned repos are stored under `repos/` (gitignored). The web UI's **Import from Repo** wizard
generates this YAML structure for you. The **Discover** tab lists curated servers with one-click
import.

## Test with local Ollama

Query the running MCP server with a local Ollama model:

```bash
curl -s http://localhost:8888/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
```

Custom Ollama integration scripts can be placed in a local (gitignored) `scripts/` directory.

## Running tests

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest tests/ -v
```

Tests cover `server.py` (pure helpers), `frontend/app.py` (all API endpoints), and
`repo_loader.py` (git/pip operations, all subprocess calls are mocked). CI runs on every push
via `.github/workflows/tests.yml`.

## Security notes

- Do not commit `.env`.
- Do not enable `debug: true` outside of local testing.
- The web UI has no authentication — run it on a trusted network only.

---

## Tutorial: adding a new tool

Every provider is a single YAML file under `tools/`. See the detailed tutorial sections below.

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

### Part 3 — multiple tools in one provider

A single YAML file can declare any number of tools sharing the same `code` block.

---

### Part 4 — error handling

Return `{"ok": True, ...}` on success, `{"ok": False, "error": "..."}` on failure. Never let an exception propagate — wrap the entire function body in `try/except`.

---

### Part 5 — calling blocking libraries with `asyncio.to_thread`

Handler functions are `async`, but many Python libraries block the event loop. Use `asyncio.to_thread()` to run them safely in a thread pool.

```python
result = await asyncio.to_thread(_fetch_sync, arg1, arg2)
```

---

### Part 6 — prompting the user mid-call (elicitation)

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

### Part 7 — persisting state between calls

Write state to a well-known file path and read it on the next call. See the Personal Capital provider for a full example using `~/.config/personalcapital2/session.json`.

---

### YAML provider reference

```yaml
documentation: |                   # optional — shown in the web UI; markdown friendly
  Describe what this provider does, its tools, secrets, and any usage notes.

repo:                              # optional — omit if code is self-contained
  url: string                      # git clone URL (required if repo block present)
  branch: main                     # optional
  subfolder: src                   # optional sub-path added to sys.path
  path: repos/override             # optional clone destination override
  requirements: requirements.txt   # optional pip file to install
  install_packages: [pkg, ...]     # optional explicit pip packages

code: |                            # Python source — executed once at startup
  # Import anything, define helpers and async tool functions.

tools:
  - name: string                   # unique MCP tool name
    function: string               # async function name from code block
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

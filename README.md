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

By default this setup runs **in the background**: the MCP server starts accepting
requests immediately while each provider's dependencies install. A tool whose provider
is still installing returns a structured **retry directive** instead of blocking — see
[Non-blocking startup](#non-blocking-startup) below.

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
| **8887** | Loopback-only OAuth callback for containerized `mcp-remote` bridges |

## Non-blocking startup

Provider setup — cloning/building repository providers, `pip install`-ing each
provider's `requirements`, and running its `setup_commands` (e.g.
`npx playwright install chrome`) — can take a long time. Rather than block the
MCP server until all of that finishes, mcpproxy **registers every tool up front
and runs the setup in the background**:

- The MCP endpoint on `8888` and the UI on `8889` come up **immediately**.
- Every tool is advertised right away, so MCP clients see the full tool list at
  once.
- A call to a tool whose provider is **still installing** returns a structured
  retry directive instead of failing or hanging:

  ```json
  {
    "ok": false,
    "tool": "playwright__browser_navigate",
    "status": "initializing",
    "retry_after_seconds": 15,
    "message": "Tool 'playwright__browser_navigate' is not ready yet — provider 'playwright' is still installing its dependencies. Wait ~15s and call this tool again."
  }
  ```

  The calling LLM reads the message and retries shortly. The two built-in tools
  (`mcpproxy__listfiles` / `mcpproxy__getfile`) are always ready immediately.

- If a provider's setup **fails**, its tools return `"status": "failed"` with the
  error at call time (the rest of the server stays up).

Knobs:

| Variable | Default | Meaning |
|---|---|---|
| `MCPPROXY_BACKGROUND_SETUP` | `1` | Set to `0` to run setup synchronously before the server starts (the old blocking behaviour). |
| `MCPPROXY_INIT_RETRY_SECONDS` | `15` | Seconds advertised in the `retry_after_seconds` field of the directive. |

Startup stays fast across restarts because pip/uv/npm caches and cloned repos are
persisted via Docker volumes — see [Volumes & caching](#volumes--caching).

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

Click **+ New Provider** and choose a provider type. Each mode card carries a short
*"Best for…"* hint with concrete examples so it's clear which one fits your situation:

| Type | Best for | Description |
|---|---|---|
| **Remote MCP Server** | Hosted SaaS tools that already speak MCP (Asana, Linear, Notion, GitHub) where you just have a URL | Paste a remote, OAuth-protected MCP server URL; mcpproxy bridges it with `npx -y mcp-remote <url>`, introspects its tools, and handles auth automatically. |
| **Package** | Published MCP servers you install and run locally (Playwright, filesystem, Slack) | Enter any command that launches a stdio MCP server (`npx`, `uvx`, `python -m`, or an installed binary). When you click **Next**, mcpproxy auto-introspects the command and pre-populates the tool list; if introspection fails you can still proceed and add tools by hand. |
| **Repository** | MCP servers distributed as source you build yourself | Provide a git URL and a list of build commands. mcpproxy clones the repo, runs the build commands, then introspects the resulting stdio MCP server. The URL and build commands are persisted in YAML so the repo can be re-cloned and re-built automatically on every container restart. |
| **REST / OAuth API** | Any plain web API with no prebuilt MCP server (Stripe, OpenWeather, internal services) | Point at a REST API: a base URL plus an OpenAPI spec (imported into tools automatically) or hand-entered endpoints, with optional OAuth. Each endpoint becomes an MCP tool. See [REST / OAuth providers](#rest--oauth-providers). |
| **Python code** | Quick custom logic, glue, or calculations you write inline | Write `async def` functions; the UI lists the ones it finds as you type. Each becomes a tool entry. |

After the provider step, the wizard shows a **Secrets** step: any `secrets.env` entries
in the provider are listed, and you can fill in their values to save them directly to `.env`.

### Browse providers catalog

The **🗂 Browse** button (next to **+ New Provider**) opens a searchable catalog of known
MCP servers and REST/OpenAPI APIs. Pick one and click **Configure →** — it opens the New
Provider wizard with the right mode selected and the URL or OpenAPI spec pre-filled, then
the usual introspection flow runs as if you'd typed it by hand.

The catalog is **hybrid**:

- A **curated list** is bundled in the repo at `frontend/catalog.json` — the offline-safe
  default. Add or edit entries there (each is either a `mcp_remote` entry with a `url`, or
  a `rest_openapi` entry with an `openapi_url`).
- Ticking **Probe live registries** also queries external sources — the official
  [MCP registry](https://registry.modelcontextprotocol.io), [Smithery](https://smithery.ai)
  (needs `SMITHERY_API_KEY`), and [APIs.guru](https://apis.guru) for OpenAPI specs. Sources
  are fetched concurrently with per-source error isolation and a short cache, so one slow or
  unavailable registry never blocks the others or the curated list.

`/api/catalog` only ever contacts that fixed set of registry hosts (it takes no caller-supplied
URL), and the server never fetches a catalog entry's own URL — that only happens through the
existing, already-guarded wizard introspection. Knobs: `MCPPROXY_CATALOG_LIVE` (set `0` to
disable live probing entirely), `MCPPROXY_CATALOG_TTL` (cache seconds, default `900`),
`MCPPROXY_CATALOG_TIMEOUT` (per-request seconds, default `8`), `MCPPROXY_CATALOG_MAX_PER_SOURCE`
(entry cap per live source, default `150`).

### Secrets manager

The **🔑 Secrets** button (also available in the wizard's final step) reads all `secrets.env`
entries from the selected provider, shows which variables are already set in `.env`, and lets
you fill in or update missing values — all without leaving the browser.

### Files manager

The **📁 Files** navbar button opens a file browser over the volume-mounted directories
(`tools`, `files`, and `repos` — i.e. `/app/tools`, `/app/files`, `/app/repos` in the
container). From the browser you can:

- **Browse** with a root selector and clickable breadcrumb navigation
- **📁 New folder** — create subdirectories (e.g. `tools/secrets/`)
- **⬆ Upload** — drop one or more files into the current directory (e.g. a Google
  `client_secret.json` for the [OAuth bootstrap](#oauth-token-file-bootstrap-oauth-block))
- **Download** any file by clicking it
- **🗑 Delete** files and directories (non-empty directories ask before deleting recursively)

All paths are validated against the whitelisted roots (directory-traversal and
symlink-escape attempts are rejected), and uploads stream to disk with a size cap
(`MCPPROXY_MAX_UPLOAD_BYTES`, default 50 MB).

### Provider status badges

While background setup is running, the left-panel provider list shows live badges:

| Badge | Meaning |
|---|---|
| **⏳ initializing** (yellow) | Provider dependencies are still installing; tools are advertised but return a retry directive until setup finishes. |
| **✗ setup failed** (red, with tooltip) | Provider setup failed. Hover the badge for the setup error. Other providers remain available. |
| *(no setup badge)* | Provider setup completed. For remote OAuth bridges, this does not by itself prove account authentication; inspect `auth_status` or call a read-only identity tool. |

The badges are updated every 4 seconds via `GET /api/provider-status`. A provider stays
marked **⏳ initializing** for as long as it needs — it is never changed to "failed" because
of timing alone; only an actual exception during setup produces the red badge.

### Registered tools list

The **📋 Tools** navbar button opens a read-only panel showing every tool currently
exposed by the proxy, grouped by provider. Each provider section shows:

- A status badge (⏳ initializing / ✓ ready / ✗ failed)
- The number of tools it contributes
- Each tool's short name and description

A filter box narrows results by tool name or description. This panel is useful for a
quick audit of what the LLM can currently see, especially during startup when some
providers may still be initializing.

Under the hood it reads the same `GET /v1/tools` endpoint as the tool tester, plus
`GET /api/provider-status` for the readiness information.

```
GET /api/provider-status
```

Returns per-provider setup state and, for `mcp-remote` bridges, authentication state:

```json
{
  "ok": true,
  "providers": {
    "playwright": {
      "status": "ready",
      "setup_status": "ready",
      "auth_status": null,
      "error": null
    },
    "asana": {
      "status": "ready",
      "setup_status": "ready",
      "auth_status": "authorization_required",
      "error": null
    }
  }
}
```

`status` remains the backward-compatible setup value: `"pending"`, `"ready"`, or
`"failed"`. `setup_status` makes that meaning explicit. `auth_status` is `null` for
ordinary providers and one of `"unknown"`, `"authorization_required"`, or
`"authenticated"` for `mcp-remote` providers. `"authenticated"` means a live MCP
initialize handshake succeeded in the current proxy process; use a read-only identity
tool for end-to-end account verification. The endpoint returns
`{"ok": true, "providers": {}}` when no background states are registered.

### Tool tester

The **🧪 Test Tools** navbar button lists every registered tool, grouped by provider,
with a filter box. Selecting a tool generates an argument form straight from its JSON
input schema — enums become dropdowns, booleans checkboxes, numbers/strings typed
inputs, and objects/arrays a raw-JSON textarea — with required/optional badges,
descriptions, and defaults pre-filled. **▶ Invoke** runs the tool and pretty-prints the
result (with error styling on failure), so you can exercise a provider end-to-end
without connecting an LLM client.

The registry is populated at server startup, so after creating or editing a provider,
restart and reopen the dialog (an empty list shows a one-click restart hint).

Under the hood the tester uses the **OpenAI-compatible tools endpoints** on the UI port,
which any OpenAI-style caller (e.g. OpenWebUI tool servers) can also use directly:

- `GET /v1/tools` — every registered tool in OpenAI function-calling schema format
- `POST /v1/tools/{tool_name}/invoke` — call a tool with `{"arguments": {...}}`;
  returns `{"type": "tool_result", "content": [...], "is_error": bool}`

### Setup Commands

Each provider has a **Setup Commands** list (editable in the editor panel, saved to YAML).
These shell commands run automatically every time the MCP server starts — perfect for
installing browser binaries, downloading data, or any one-time setup that must survive a
Docker restart.

Example — for a Playwright package provider:
```
npx playwright install chrome
```

Commands run in order in the background (see [Non-blocking startup](#non-blocking-startup)) —
the server accepts connections immediately and the provider's tools return a retry directive
until its setup finishes. The subprocess package itself is launched lazily on the first tool
call, so the browser binary is always ready when needed.

> **After editing and saving** a provider's command or setup steps, click **Restart MCP Server**
> (the yellow bar that appears after saving) to apply the changes.

## REST / OAuth providers

A **REST provider** wraps an HTTP/REST API directly — no Python and no separate MCP
server needed. A provider YAML with a `rest:` block declares a base URL, an `auth:`
block, and a set of endpoints; each endpoint becomes an MCP tool. mcpproxy builds the
HTTP request (path/query/body), attaches authentication, and returns the JSON response.

Create one through the **+ New Provider → REST / OAuth API** wizard. You can **import an
OpenAPI spec** (URL or file — OpenAPI 3.x or Swagger 2.0) to generate the endpoints and tools automatically, or
enter endpoints by hand. OpenAPI specs are expanded into concrete endpoints when the
provider is created, so startup stays fast and offline.

After creation, the editor lets you **edit everything inline** — the base URL, the auth
block, default headers (sent on every request), and the endpoint list (method, path, and
which params go in the path / query / body). Adding or removing an endpoint keeps its
paired tool in sync (endpoints map 1:1 to tools by name), and **⟳ Sync params to tool
schema** regenerates a tool's input schema from its endpoint's params.

Large responses are **truncated** to a bounded preview (with a `truncated` flag) so a
single call can't flood the model's context — tune or disable via `MCPPROXY_REST_MAX_BYTES`.

### Authentication

The `auth.type` field selects how requests are authenticated. Secrets are referenced by
**environment-variable name** (the `*_env` fields) and filled in via the Secrets UI / `.env` —
never written into the YAML.

| `auth.type` | Fields | Behaviour |
|---|---|---|
| `none` | — | No authentication. |
| `bearer` | `token_env` | Sends `Authorization: Bearer <env>`. |
| `api_key` | `value_env`, plus either `header` (default `X-Api-Key`) or `in: query` + `name` | Sends the secret in a custom header, or as a query parameter when `in: query`. |
| `client_credentials` | `token_url`, `client_id_env`, `client_secret_env`, `scopes` | OAuth2 client-credentials. Token is fetched, cached, and auto-refreshed on expiry/401. |
| `authorization_code` | `authorize_url`, `token_url`, `client_id_env`, `client_secret_env` (optional for PKCE), `scopes` | Interactive OAuth2 + PKCE. Click **🔐 Authorize** in the editor to complete the browser flow; tokens are cached and refreshed automatically. |

For `authorization_code`, register the redirect URI **`<MCPPROXY_OAUTH_REDIRECT_BASE>/oauth/callback`**
(default `http://localhost:8889/oauth/callback`) with your OAuth provider. Tokens are cached
under `MCPPROXY_REST_AUTH_DIR` (default `/app/.rest-auth`, gitignored).

### Example

```yaml
rest:
  base_url: https://api.example.com/v1
  headers:
    Accept: application/json
  auth:
    type: client_credentials
    token_url: https://auth.example.com/oauth/token
    client_id_env: EXAMPLE_CLIENT_ID
    client_secret_env: EXAMPLE_CLIENT_SECRET
    scopes: [read, write]
  endpoints:
    - name: get_user
      method: GET
      path: /users/{user_id}
      path_params: [user_id]
      query_params: [include]
      body_params: []
    - name: create_item
      method: POST
      path: /items
      path_params: []
      query_params: []
      body_params: [title, body]

requirements: [httpx]

tools:
  - name: get_user
    description: Fetch a user by id.
    input_schema:
      type: object
      properties:
        user_id: {type: string}
        include: {type: string}
      required: [user_id]
  - name: create_item
    description: Create an item.
    input_schema:
      type: object
      properties:
        title: {type: string}
        body:  {type: string}
      required: [title]
```

Each tool's `name` maps 1:1 to an endpoint's `name`. REST providers depend on `httpx`
(installed by default).

At startup, OAuth-backed REST providers are **warmed**: `client_credentials` tokens are
fetched and cached, and `authorization_code` providers that have no usable token surface
their **🔐 Authorize** link in the banner immediately, rather than only after the first
failed tool call. (Disable with `MCPPROXY_WARM_REMOTE=0`.)

Config knobs: `MCPPROXY_REST_AUTH_DIR`, `MCPPROXY_OAUTH_REDIRECT_BASE`,
`MCPPROXY_REST_TIMEOUT` (per-request HTTP timeout), `MCPPROXY_REST_MAX_BYTES` (max
response size before truncation; 0 disables), and `MCPPROXY_OAUTH_FLOW_TTL` (seconds an
in-flight authorization attempt stays valid; default 600).

### OAuth token-file bootstrap (`oauth:` block)

Some providers — typically **code providers** using Google client libraries — need a
user-consent OAuth token *file* (e.g. `gmail_token.json` minted from
`client_secret.json`) rather than header injection. Instead of running
`InstalledAppFlow` on a machine with a browser and copying the files in, declare the
need in the provider YAML and let mcpproxy run the flow:

```yaml
oauth:
  type: google            # the only supported type today
  client_secret_file: /app/tools/secrets/client_secret.json
  token_file: /app/tools/secrets/gmail_token.json
  scopes:
    - https://www.googleapis.com/auth/gmail.settings.basic
    - https://www.googleapis.com/auth/gmail.labels
  # optional: prompt (default "consent"), login_hint
```

How it works:

1. Create a Google OAuth client (Desktop **installed** type is easiest) in the Google
   Cloud Console, download `client_secret.json`, and upload it via the **📁 Files**
   manager (e.g. into `tools/secrets/`).
2. At startup (and via the **🔐 Authorize** button in the provider editor), mcpproxy
   checks `token_file`. If no usable token exists, the consent URL — built with PKCE,
   `access_type=offline`, and `prompt=consent` — appears in the yellow pending-auth
   banner.
3. Click it, approve in Google, and the browser is redirected to mcpproxy's
   `/oauth/callback`, which exchanges the code and writes `token_file` in exactly the
   format `google.oauth2.credentials.Credentials.from_authorized_user_file()` accepts.
4. Your provider code reads the token file as usual; the Google client libraries refresh
   the access token automatically from the stored `refresh_token` at call time. If the
   token is ever revoked, the 🔐 badge and banner reappear — re-authorizing is one click.

Redirect URI: Desktop ("installed") Google clients accept `http://localhost:8889/oauth/callback`
without registration (browse the UI via localhost when authorizing). "Web" clients must
have the exact URI registered — set `MCPPROXY_OAUTH_REDIRECT_BASE` if the UI is served
from a different origin. Note `prompt=consent` is the default because Google only issues
a refresh_token on a full consent screen, not on silent re-approval.

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
  -p 8888:8888 -p 8889:8889 -p 127.0.0.1:8887:8887 \
  --env-file .env \
  -e MCP_REMOTE_CONFIG_DIR=/app/.mcp-auth \
  -e MCPPROXY_CALLBACK_FORWARD_PORTS=8887 \
  -v "$(pwd)/tools":/app/tools \
  --name mcpproxy \
  ghcr.io/billjr99/mcpproxy:latest
```

**Run with persistent caches and artefacts** — add named volumes so cloned repos,
package caches, and provider output files survive container restarts:

```bash
docker run -d --rm \
  -p 8888:8888 -p 8889:8889 -p 127.0.0.1:8887:8887 \
  --env-file .env \
  -e MCP_REMOTE_CONFIG_DIR=/app/.mcp-auth \
  -e MCPPROXY_CALLBACK_FORWARD_PORTS=8887 \
  -v "$(pwd)/tools":/app/tools \
  -v mcpproxy-files:/app/files \
  -v mcpproxy-repos:/app/repos \
  -v mcpproxy-cache:/root/.cache \
  -v mcpproxy-npm:/root/.npm \
  -v mcpproxy-uv-tools:/root/.local/share/uv \
  -v mcpproxy-mcp-auth:/app/.mcp-auth \
  -v mcpproxy-rest-auth:/app/.rest-auth \
  -e MCP_REMOTE_CONFIG_DIR=/app/.mcp-auth \
  --name mcpproxy \
  ghcr.io/billjr99/mcpproxy:latest
```

The `mcpproxy-mcp-auth` volume holds the OAuth token cache for `mcp-remote` bridge
providers (e.g. the official Asana MCP). Persist it and you authorize once, then the
token refreshes silently; drop it and you re-authorize on every fresh container. The
callback is published only on host loopback (`127.0.0.1:8887`) and forwarded inside
the container to `mcp-remote`'s loopback-only listener. Omit the callback mapping and
forwarder setting if you have no OAuth-bridge providers.

**Safe image upgrades:** pull `ghcr.io/billjr99/mcpproxy:latest`, recreate the
container with the same mounts, named volumes, environment settings, and port mappings,
then verify ports `8888`, `8889`, and loopback-only `8887`. Reusing
`mcpproxy-mcp-auth:/app/.mcp-auth` is what preserves OAuth authorization across the
replacement. Confirm the deployed source revision without reading environment values:

```bash
docker inspect mcpproxy \
  --format '{{index .Config.Labels "org.opencontainers.image.revision"}}'
docker ps --filter name=mcpproxy \
  --format '{{.Names}} {{.State}} {{.Ports}}'
```

If a stopped old container is retained temporarily for rollback, disable its restart
policy or remove it after verification. Host watchdogs that start every stopped
container can otherwise restart the rollback copy and cause port conflicts.

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
  -p 8888:8888 -p 8889:8889 -p 127.0.0.1:8887:8887 \
  --env-file "$HOME/.mcpproxy/.env" \
  -e MCP_ENV_FILE=/app/.env \
  -e MCP_REMOTE_CONFIG_DIR=/app/.mcp-auth \
  -e MCPPROXY_CALLBACK_FORWARD_PORTS=8887 \
  -v "$HOME/.mcpproxy/tools:/app/tools" \
  -v "$HOME/.mcpproxy/.env:/app/.env" \
  -v mcpproxy-files:/app/files \
  -v mcpproxy-repos:/app/repos \
  -v mcpproxy-cache:/root/.cache \
  -v mcpproxy-npm:/root/.npm \
  -v mcpproxy-uv-tools:/root/.local/share/uv \
  -v mcpproxy-mcp-auth:/app/.mcp-auth \
  -v mcpproxy-rest-auth:/app/.rest-auth \
  --name mcpproxy \
  ghcr.io/billjr99/mcpproxy:latest
```

The `mcpproxy-rest-auth` volume persists OAuth tokens for REST `authorization_code`
providers (see [REST / OAuth providers](#rest--oauth-providers)) so you authorize once
rather than on every fresh container. Omit it if you don't use REST OAuth providers.

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

> **Can't (or don't want to) bind-mount the `.env` file directly?** Some setups — rootless
> Docker, SELinux, or hosts where binding a single non-existent file silently creates a
> *directory* — make a file-level mount awkward. In that case bind-mount the **directory**
> instead and point `MCP_ENV_FILE` at the file inside it. Mount the directory somewhere
> outside `/app` (so it doesn't shadow the image's contents) and set `MCP_ENV_FILE`
> accordingly:
>
> ```bash
> docker run -d \
>   --restart unless-stopped \
>   -p 8888:8888 -p 8889:8889 -p 127.0.0.1:8887:8887 \
>   --env-file "$HOME/.mcpproxy/.env" \
>   -e MCP_ENV_FILE=/run/secrets/mcpproxy.env \
>   -e MCP_REMOTE_CONFIG_DIR=/app/.mcp-auth \
>   -e MCPPROXY_CALLBACK_FORWARD_PORTS=8887 \
>   -v "$HOME/.mcpproxy/tools:/app/tools" \
>   -v "$HOME/.mcpproxy/.env:/run/secrets/mcpproxy.env:ro" \
>   -v mcpproxy-files:/app/files \
>   -v mcpproxy-repos:/app/repos \
>   -v mcpproxy-cache:/root/.cache \
>   -v mcpproxy-npm:/root/.npm \
>   -v mcpproxy-uv-tools:/root/.local/share/uv \
>   -v mcpproxy-mcp-auth:/app/.mcp-auth \
>   -v mcpproxy-rest-auth:/app/.rest-auth \
>   --name mcpproxy \
>   ghcr.io/billjr99/mcpproxy:latest
> ```
>
> To mount the whole directory rather than the single file, replace the
> `-v "$HOME/.mcpproxy/.env:/run/secrets/mcpproxy.env:ro"` line with
> `-v "$HOME/.mcpproxy:/run/secrets:ro"` (the `.env` then appears at
> `/run/secrets/.env`, so set `-e MCP_ENV_FILE=/run/secrets/.env`). Note that a read-only
> (`:ro`) mount means the web UI's **🔑 Secrets** panel can't write changes back; drop `:ro`
> if you want live edits to persist.

The `mcpproxy-mcp-auth` volume holds the OAuth token cache for `mcp-remote` bridge
providers (e.g. the official Asana MCP); persist it and you authorize once. Keep the
callback mapping bound to host loopback. Omit any volume you don't need; each falls
back to the container's ephemeral writable layer.

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

`docker-compose.yml` declares eight named volumes. Only the first is required —
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
| `/app/.rest-auth` | `mcpproxy-rest-auth` | OAuth token cache for REST `authorization_code` providers. | Re-authorize REST OAuth providers on every fresh container. |

The image pins `PIP_CACHE_DIR=/root/.cache/pip` and `UV_CACHE_DIR=/root/.cache/uv`
so the pip and uv wheel caches always land inside the persisted `mcpproxy-cache`
volume, even if `HOME`/XDG defaults change.

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

Some MCP servers are remote, OAuth-protected HTTP endpoints rather than stdio packages.
The official Asana V2 MCP endpoint is `https://mcp.asana.com/v2/mcp`. mcpproxy reaches it
through the [`mcp-remote`](https://www.npmjs.com/package/mcp-remote) stdio bridge.

Asana V2 does **not** support dynamic client registration for custom clients. Create an
Asana MCP app first, register the exact callback URI
`http://localhost:8887/oauth/callback`, and keep its app credentials in an untracked file:

```text
tools/
└── secrets/
    └── asana/
        └── client_info.json
```

```json
{
  "client_id": "<ASANA_MCP_CLIENT_ID>",
  "client_secret": "<ASANA_MCP_CLIENT_SECRET>"
}
```

Set the file to owner-readable only (`chmod 600` on POSIX hosts). Never put the values in
the provider YAML, command line, image, README, or Git. The `tools/` tree is gitignored by
this repository and must be mounted at runtime.

Use a pinned bridge version, the static client-info file, an explicit protected resource,
and the fixed callback port:

```yaml
package:
  command: >-
    npx -y mcp-remote@0.1.38
    https://mcp.asana.com/v2/mcp
    8887
    --static-oauth-client-info @/app/tools/secrets/asana/client_info.json
    --resource https://mcp.asana.com/v2
    --auth-timeout 600

tools:
  - name: get_me      # advertised as asana__get_me; add/introspect the remaining tools
    description: Return the Asana user that the authorized token belongs to.
    input_schema: { type: object, properties: {} }
```

`mcp-remote` reads the JSON file directly, so the client secret does not appear in the
process argument list. On first use it prints an authorization URL and waits for the
callback. After authorization it refreshes the access token automatically.

#### Docker callback forwarding

`mcp-remote@0.1.38` intentionally listens on `127.0.0.1` inside the container. Ordinary
Docker port publishing reaches the container's non-loopback interface, so a mapping alone
cannot reach that listener. mcpproxy's callback forwarder bridges the published container
interface port to the same port on container loopback.

The deployment must include both settings:

```bash
-p 127.0.0.1:8887:8887 \
-e MCPPROXY_CALLBACK_FORWARD_PORTS=8887
```

Keep the host mapping on `127.0.0.1`; do not publish OAuth callback ports on all interfaces.
`docker-compose.yml` includes the mapping and forwarder setting by default.

#### Persisting the token cache

Set `MCP_REMOTE_CONFIG_DIR=/app/.mcp-auth` and mount the `mcpproxy-mcp-auth` volume there.
This directory contains live access and refresh tokens and is intentionally separate from
`/app/files` and `tools/secrets`. Keep the volume across container recreation so a new image
does not require another grant.

#### One-time authorization and headless use

1. Trigger an Asana tool or let startup warm the bridge. mcpproxy surfaces the authorization
   link in the pending-auth UI without writing the sensitive URL to normal logs.
2. Open the link in a browser running on the same machine as Docker, approve access, and let
   Asana redirect to `http://localhost:8887/oauth/callback`.
3. If authorization is completed on another device, its `localhost` is that device, not the
   Docker host. Copy the final callback URL into a browser on the Docker host while the bridge
   is still waiting. Treat that URL as a secret because it contains a short-lived code.
4. Verify `asana__get_me`, then recreate the container with the same auth volume and verify it
   again without reauthorization.

On startup mcpproxy warms configured `mcp-remote` bridges. With a valid cache, refresh is
silent. If authorization is required, the UI shows a pending-auth banner. A provider's
**ready** setup badge means dependencies loaded successfully; it does not by itself prove
that the remote account is authenticated. Verify authentication with a harmless read-only
tool such as `get_me`.

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


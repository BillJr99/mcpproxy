# mcpproxy: Config-Driven MCP Host

This project is a Dockerized, config-driven MCP server. Each tool **provider** is a single YAML file under `config/tools/`. The YAML contains:

- The Python code for all tool functions (embedded directly in the file)
- One or more tool declarations that reference those functions
- Per-tool input schemas, secrets, and auth metadata

`server.py` loads every YAML at startup, executes its `code` block, and registers each declared tool automatically — no Python files to maintain separately, no changes to `server.py` needed when adding new tools.

The included example exposes Empower Personal Capital data through `personalcapital2`:

| Tool | What it returns |
|---|---|
| `personalcapital_get_accounts` | Account list with open/closed status clearly marked on every entry |
| `personalcapital_get_transactions` | Recent transactions with account names resolved; closed-account transactions labelled |

Both tools support MCP elicitation for SMS two-factor authentication and include a portable fallback for clients that don't.

## Layout

```text
.
├── Dockerfile
├── docker-compose.yml              ← base: named volumes (prod/CI)
├── docker-compose.override.yml     ← dev: bind mounts (auto-merged locally)
├── requirements.txt
├── server.py
├── .env.example
├── config/
│   └── tools/
│       └── personalcapital.yaml    ← provider: code + tool declarations
├── handlers/
│   └── elicitation.py              ← shared mid-call input helper
└── scripts/
    ├── run_local.sh
    ├── test_with_ollama.sh
    └── test_personalcapital_ollama.sh
```

## Secrets

Run `./scripts/run_local.sh` to be prompted for all required values. It generates `.env.example` from the YAML tool files if it doesn't already exist, then writes `.env` with the values you enter.

Or do it manually:

```bash
cp .env.example .env
# edit .env and fill in PERSONALCAPITAL_EMAIL and PERSONALCAPITAL_PASSWORD
```

The `.env` file is consumed by Docker Compose via `env_file`. Credentials are never part of the MCP tool schema, so they are not exposed as LLM-visible tool arguments.

## Run locally

```bash
./scripts/run_local.sh
```

The script will:
1. Generate `.env.example` from the YAML tool files if it doesn't exist.
2. Prompt for any missing or placeholder values and write `.env`.
3. Override `MCP_TOOL_CONFIG_DIR` to the correct local path (the Docker path stored in `.env` is not used locally).
4. Create `.venv`, install dependencies, and start the server.

## Run with Docker

The MCP endpoint is always at **`http://localhost:8888/mcp`**.

`config/` and `handlers/` are never baked into the image — they are supplied at runtime via volume mounts. `.env` is loaded by Docker Compose via `env_file` and is never copied into the image either.

### Local development (bind mounts)

`docker-compose.override.yml` is merged automatically when you run `docker compose` without a `-f` flag. It replaces the named volumes in the base file with direct bind mounts of your local `./config/tools` and `./handlers` directories, so edits are reflected without a rebuild.

```bash
# First run: build the image and start
docker compose up --build

# Subsequent runs (image already built, no code changes to server.py/deps)
docker compose up

# Rebuild the image after changing server.py or requirements.txt
docker compose up --build

# Run in the background
docker compose up -d

# Follow logs
docker compose logs -f

# Stop
docker compose down
```

The bind-mount volumes are **read-only** (`:ro`). If you modify a YAML tool file, restart the container to pick up the change:

```bash
docker compose restart mcp-host
```

### Production / CI (named volumes)

The base `docker-compose.yml` uses named Docker volumes (`mcpproxy-config`, `mcpproxy-handlers`) instead of bind mounts. This lets you run the container without keeping the repo checked out on the host.

Populate the volumes once before the first run (rerun whenever you update files):

```bash
docker run --rm \
  -v mcpproxy-config:/dst \
  -v "$(pwd)/config/tools":/src:ro \
  alpine sh -c "cp -r /src/. /dst/"

docker run --rm \
  -v mcpproxy-handlers:/dst \
  -v "$(pwd)/handlers":/src:ro \
  alpine sh -c "cp -r /src/. /dst/"
```

Then start with only the base file (skips the override):

```bash
docker compose -f docker-compose.yml up -d
```

### Environment variables and secrets

Copy `.env.example` to `.env` and fill in your secrets before starting Docker Compose:

```bash
cp .env.example .env
# edit .env — set PERSONALCAPITAL_EMAIL, PERSONALCAPITAL_PASSWORD, etc.
```

Or run `./scripts/run_local.sh` once — it writes `.env` interactively and Docker Compose will use the same file.

Docker Compose reads `.env` via `env_file:` at container start time. The file is **never copied into the image**. Do not commit `.env`.

### Port

The server listens on **port 8888** by default. To use a different host port without editing files:

```bash
MCP_HOST_PORT=9000 docker compose up   # map host:9000 → container:8888
```

Add `MCP_HOST_PORT` to `docker-compose.yml` if you want to make this permanent:

```yaml
ports:
  - "${MCP_HOST_PORT:-8888}:8888"
```

## Test with local Ollama

```bash
bash scripts/test_with_ollama.sh
```

Queries Ollama for available models and presents a numbered menu. Select a model, and it will send `initialize` → `tools/list` and print the URL, request body, and response for every MCP call.

```bash
OLLAMA_MODEL=qwen3:14b bash scripts/test_with_ollama.sh
```

To test the Personal Capital tools specifically (with real credentials and 2FA):

```bash
bash scripts/test_personalcapital_ollama.sh
```

This calls `personalcapital_get_accounts` and `personalcapital_get_transactions`, handles SMS 2FA interactively, displays results in tabular form, and asks Ollama to summarise your finances.

## Tool: `personalcapital_get_accounts`

Returns the account list.

```json
{
  "ok": true,
  "accounts": [
    {
      "user_account_id": 12345,
      "name": "Checking Account",
      "institution": "Chase",
      "account_type": "CHECKING",
      "balance": 1234.56,
      "status": "open",
      "is_closed": false
    },
    {
      "user_account_id": 99999,
      "name": "Old Savings",
      "institution": "Bank of America",
      "account_type": "SAVINGS",
      "balance": 0.0,
      "status": "closed",
      "is_closed": true
    }
  ],
  "metadata": {
    "include_closed": true,
    "total_count": 2,
    "open_count": 1,
    "closed_count": 1
  }
}
```

Closed accounts are included only when `include_closed: true`. Every entry carries `status: "open"` or `status: "closed"`.

## Tool: `personalcapital_get_transactions`

Returns recent transactions with account names resolved. Accounts are fetched internally so the tool can always resolve names — including for transactions that belong to closed accounts.

```json
{
  "ok": true,
  "transactions": [
    {
      "date": "2025-05-10",
      "description": "Coffee Shop",
      "amount": -4.50,
      "category": "Food & Dining",
      "merchant": "Blue Bottle Coffee",
      "user_account_id": 12345,
      "account_name": "Checking Account",
      "account_status": "open"
    },
    {
      "date": "2025-04-01",
      "description": "Final transfer",
      "amount": -500.00,
      "category": "Transfer",
      "merchant": null,
      "user_account_id": 99999,
      "account_name": "Old Savings",
      "account_status": "closed"
    }
  ],
  "metadata": {
    "days": 90,
    "start_date": "2025-02-23",
    "end_date": "2025-05-24",
    "include_closed": true,
    "transaction_count": 2
  }
}
```

`account_status` on each transaction is `"open"`, `"closed"`, or `"unknown"`.

## Two-factor behavior

Personal Capital uses an exception-based SMS two-factor flow. When 2FA is required:

1. The first call triggers `TwoFactorRequiredError` internally.
2. The server sends an SMS challenge and persists the session state to `~/.config/personalcapital2/session.json`.
3. From there, the 2FA path depends on what the MCP client supports:

**If the client supports elicitation** — the server prompts for the code mid-call using `ctx.elicit()`. The user enters the code and the same tool call completes without a second invocation.

**If the client does not support elicitation** — the server returns:

```json
{
  "ok": false,
  "needs_input": true,
  "input": {
    "name": "sms_code",
    "description": "Enter the SMS two-factor code sent by Personal Capital.",
    "type": "string"
  }
}
```

The caller then invokes the same tool again with `sms_code` set. The session file on disk bridges the two calls: it holds the CSRF token written after the first call, and the second call loads it to verify the code.

**Session sharing with the `pc2` CLI** — the session file is the same one used by the `pc2` command-line tool. If you have already run `pc2 login` and your device is remembered by the server, neither the MCP server nor the CLI will require 2FA again until the session expires.

## Security notes

- Do not commit `.env`.
- Do not enable `debug: true` outside of local testing. Known secret-looking fields are redacted, but provider schemas can change.

---

## Tutorial: adding a new tool

Every provider is a single YAML file under `config/tools/`. It has two top-level keys:

| Key | Purpose |
|---|---|
| `code` | Python source executed once at startup. Define all helper functions and async tool functions here. |
| `tools` | List of MCP tool declarations. Each entry names one `async` function from `code` and declares its schema and secrets. |

`server.py` reads every YAML in `config/tools/` at startup and registers each declared tool automatically — no changes to `server.py` are needed.

---

### Part 1 — a simple tool with no secrets

This example adds a `ping` tool that echoes a message back with a timestamp.

#### Step 1 — create `config/tools/ping.yaml`

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

That's the whole thing — one file, no separate handler module.

**Rules for functions in the `code` block:**

- Must be `async def`.
- First argument must be `context` (a `dict` injected by the server — see [the context object](#the-context-object)).
- Remaining arguments map 1-to-1 to the `input_schema.properties` keys in the `tools` entry below.
- Return a plain JSON-serialisable `dict`.

**The `tools` list** entries have these fields:

| Field | Required | Purpose |
|---|---|---|
| `name` | ✓ | Unique MCP tool name |
| `function` | ✓ | Name of the `async` function defined in `code` |
| `description` | ✓ | Description shown to the LLM |
| `input_schema` | ✓ | JSON Schema object — drives LLM argument generation |
| `secrets.env` | — | Maps handler arg names to environment variable names |
| `auth` | — | Arbitrary dict forwarded to `context["auth"]` |

#### Step 2 — restart and test

```bash
./scripts/run_local.sh
```

The server prints a line for every tool it loads:

```
Registered tool: ping
```

Verify with `curl`:

```bash
curl -s -X POST http://localhost:8888/mcp \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0", "id": 1, "method": "tools/call",
    "params": {"name": "ping", "arguments": {"message": "world"}}
  }'
```

---

### Part 2 — a tool with injected secrets

This example adds a `get_weather` tool. The pattern is the same for any API that requires a key.

> **Why not pass secrets as tool arguments?**  
> Secrets that appear in `input_schema` become visible to the LLM. The `secrets.env` block in the YAML injects them server-side from environment variables; the LLM never sees them and they never travel over the MCP wire.

#### Step 1 — create `config/tools/weather.yaml`

```yaml
code: |
  import urllib.request
  import json
  import traceback
  from typing import Any

  async def get_weather(
      context: dict[str, Any],
      latitude: float,
      longitude: float,
      api_key: str,          # injected from WEATHER_API_KEY — not in the LLM schema
  ) -> dict[str, Any]:
      try:
          url = (
              f"https://api.open-meteo.com/v1/forecast"
              f"?latitude={latitude}&longitude={longitude}&current_weather=true"
          )
          with urllib.request.urlopen(url, timeout=10) as response:
              data = json.loads(response.read())
          current = data.get("current_weather", {})
          return {
              "ok": True,
              "latitude": latitude,
              "longitude": longitude,
              "temperature_c": current.get("temperature"),
              "windspeed_kmh": current.get("windspeed"),
              "weather_code": current.get("weathercode"),
          }
      except Exception as e:
          traceback.print_exc()
          return {"ok": False, "error": str(e)}

tools:
  - name: get_weather
    function: get_weather
    description: >
      Return the current weather at a latitude/longitude coordinate.
      The API key is injected server-side and is not exposed to the LLM.
    input_schema:
      type: object
      properties:
        latitude:
          type: number
          description: Latitude of the location.
        longitude:
          type: number
          description: Longitude of the location.
      required:
        - latitude
        - longitude
    secrets:
      env:
        api_key: WEATHER_API_KEY   # handler arg: env var name
```

The `secrets.env` block maps handler argument names to environment variable names. The argument (`api_key`) is injected by the server before calling the function — the LLM only sees `latitude` and `longitude`.

#### Step 2 — add the secret to `.env.example` and `.env`

Append to `.env.example` (commit this):

```
# API key for the weather service
WEATHER_API_KEY=replace-me
```

Or just run `./scripts/run_local.sh` — it reads all `secrets.env` blocks from every YAML and prompts for any variables that are missing from `.env`.

#### Step 3 — restart and test

```bash
./scripts/run_local.sh
```

```bash
curl -s -X POST http://localhost:8888/mcp \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0", "id": 1, "method": "tools/call",
    "params": {
      "name": "get_weather",
      "arguments": {"latitude": 37.77, "longitude": -122.42}
    }
  }'
```

---

### Part 3 — multiple tools in one provider

A single YAML file can declare any number of tools, all sharing the same `code` block. Use this pattern when:

- Multiple tools share helpers or data structures.
- One tool needs data fetched by another (e.g. `get_transactions` fetches accounts internally for name resolution).
- You want to keep related logic together.

This example adds two math tools — `calculator_basic` and `calculator_stats` — that share a validation helper.

#### Create `config/tools/calculator.yaml`

```yaml
code: |
  from typing import Any

  # ── shared helper ────────────────────────────────────────────────────────────

  def _require_numbers(values: list, label: str = "values") -> list[float]:
      """Raise ValueError if any item in values is not a real number."""
      result = []
      for i, v in enumerate(values):
          if not isinstance(v, (int, float)):
              raise ValueError(f"{label}[{i}] is not a number: {v!r}")
          result.append(float(v))
      return result

  # ── tool 1 ───────────────────────────────────────────────────────────────────

  async def basic(
      context: dict[str, Any],
      operation: str,
      a: float,
      b: float,
  ) -> dict[str, Any]:
      ops = {
          "add":      lambda x, y: x + y,
          "subtract": lambda x, y: x - y,
          "multiply": lambda x, y: x * y,
          "divide":   lambda x, y: x / y,
      }
      if operation not in ops:
          return {"ok": False, "error": f"Unknown operation '{operation}'. Choose: {list(ops)}"}
      if operation == "divide" and b == 0:
          return {"ok": False, "error": "Division by zero."}
      return {"ok": True, "operation": operation, "a": a, "b": b, "result": ops[operation](a, b)}

  # ── tool 2 ───────────────────────────────────────────────────────────────────

  async def stats(
      context: dict[str, Any],
      values: list,
  ) -> dict[str, Any]:
      try:
          nums = _require_numbers(values)
      except ValueError as e:
          return {"ok": False, "error": str(e)}
      if not nums:
          return {"ok": False, "error": "values list is empty."}
      return {
          "ok":    True,
          "count": len(nums),
          "min":   min(nums),
          "max":   max(nums),
          "sum":   sum(nums),
          "mean":  sum(nums) / len(nums),
      }

tools:
  - name: calculator_basic
    function: basic
    description: Perform a single arithmetic operation (add, subtract, multiply, divide).
    input_schema:
      type: object
      properties:
        operation:
          type: string
          description: "One of: add, subtract, multiply, divide"
        a:
          type: number
          description: First operand.
        b:
          type: number
          description: Second operand.
      required:
        - operation
        - a
        - b

  - name: calculator_stats
    function: stats
    description: Return min, max, mean, and sum for a list of numbers.
    input_schema:
      type: object
      properties:
        values:
          type: array
          items:
            type: number
          description: List of numbers to analyse.
      required:
        - values
```

Both tools appear separately in `tools/list`, but `_require_numbers` is shared — defined once in `code` and called from either function.

#### Restart and test

```bash
./scripts/run_local.sh
```

```bash
# Basic arithmetic
curl -s -X POST http://localhost:8888/mcp \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0", "id": 1, "method": "tools/call",
    "params": {"name": "calculator_basic",
               "arguments": {"operation": "multiply", "a": 6, "b": 7}}
  }'
# → {"ok": true, "result": 42, …}

# Descriptive stats
curl -s -X POST http://localhost:8888/mcp \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0", "id": 1, "method": "tools/call",
    "params": {"name": "calculator_stats",
               "arguments": {"values": [3, 1, 4, 1, 5, 9, 2, 6]}}
  }'
# → {"ok": true, "count": 8, "min": 1.0, "max": 9.0, "mean": 3.875, "sum": 31.0}
```

The Personal Capital provider in this project follows the same pattern at larger scale:

```
config/tools/personalcapital.yaml
  code:
    ├── async def get_accounts(...)       ← tools[0]: personalcapital_get_accounts
    ├── async def get_transactions(...)   ← tools[1]: personalcapital_get_transactions
    └── (shared: _authenticate, _normalize_accounts, redact_secrets, …)
  tools:
    ├── personalcapital_get_accounts
    └── personalcapital_get_transactions
```

---

### Part 4 — error handling

Every handler should follow the same error contract so that callers can handle failures uniformly.

**The contract:** return `{"ok": True, ...}` on success, `{"ok": False, "error": "..."}` on failure. Never let an exception propagate — wrap the entire function body in `try/except` and return the error as data.

#### Pattern: top-level try/except with server-side logging

```python
async def my_tool(context: dict[str, Any], query: str) -> dict[str, Any]:
    try:
        result = _do_work(query)
        return {"ok": True, "result": result}

    except ValueError as e:
        # Expected error — no traceback needed
        return {"ok": False, "error": str(e)}

    except Exception as e:
        # Unexpected error — log full traceback to server stdout
        import traceback
        print(f"my_tool error: {e}")
        traceback.print_exc()
        return {"ok": False, "error": str(e)}
```

`traceback.print_exc()` writes to server stdout (visible via `docker compose logs` or the local terminal). The caller only sees the clean error string.

#### Pattern: redact secrets in debug output

If your handler returns raw API responses in a `debug` mode, strip fields whose names contain sensitive keywords before returning:

```python
SECRET_KEYS = {"password", "token", "secret", "api_key", "apikey", "authorization"}

def redact_secrets(value):
    if isinstance(value, dict):
        return {
            k: "[REDACTED]" if any(s in k.lower() for s in SECRET_KEYS) else redact_secrets(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    return value
```

---

### Part 5 — calling blocking libraries with `asyncio.to_thread`

Handler functions are `async`, but many Python libraries (database drivers, synchronous HTTP clients, third-party SDKs) block the event loop. Calling them directly will stall the entire MCP server.

**The fix:** `asyncio.to_thread()` runs a blocking callable in a worker thread without blocking the event loop.

#### When to use it

Use `asyncio.to_thread` whenever the function you're calling:
- Does network I/O without async support (e.g. `requests`, `urllib`)
- Uses a blocking database driver
- Wraps a C extension or third-party SDK with no async version
- Does significant CPU-bound work

#### Example: wrapping a sync SDK call

```python
import asyncio
import traceback
from typing import Any

# Synchronous — blocks while it fetches data.
def _fetch_sync(api_key: str, query: str) -> dict:
    import some_blocking_sdk
    client = some_blocking_sdk.Client(api_key)
    return client.search(query)

async def search_data(
    context: dict[str, Any],
    query: str,
    api_key: str,    # injected from secrets.env
) -> dict[str, Any]:
    try:
        # Run the blocking call in a thread pool.
        result = await asyncio.to_thread(_fetch_sync, api_key, query)
        return {"ok": True, "results": result}
    except Exception as e:
        traceback.print_exc()
        return {"ok": False, "error": str(e)}
```

The async function (what the server calls) does only two things: dispatch the blocking work with `asyncio.to_thread`, and handle the result. All SDK interaction stays in the sync helper.

#### How `personalcapital.yaml` uses this pattern

```python
async def get_accounts(context, email, password, ...) -> dict:
    result = await asyncio.to_thread(
        _fetch_accounts_sync, email, password, include_closed, debug, sms_code
    )
    ...

def _fetch_accounts_sync(email, password, include_closed, debug, sms_code) -> dict:
    from personalcapital2 import EmpowerClient
    client = EmpowerClient(session_path=PC_SESSION_PATH)
    # All sync SDK calls happen here, safely in a thread.
    auth_result = _authenticate(client, email, password, sms_code)
    ...
```

---

### Part 6 — prompting the user mid-call (elicitation)

Some tools need input that isn't known until the call is already running — for example, an SMS two-factor code that arrives after the initial login attempt.

MCP supports **elicitation**: a tool call can pause and ask the client for more information. The shared helper in `handlers/elicitation.py` tries elicitation first and falls back gracefully for clients that don't support it.

#### Import the helper in your `code` block

```python
from handlers.elicitation import request_text_input_with_fallback
```

#### Call it when you need input

```python
sms_result = await request_text_input_with_fallback(
    context=context,          # the context dict passed to your handler
    field_name="sms_code",    # name of the field to collect
    message="We sent an SMS to your phone.",   # shown to the user
    description="Enter the six-digit code.",   # field label / description
)
```

Return values:

| `sms_result` contains | Meaning |
|---|---|
| `{"ok": True, "value": "123456", "source": "elicitation"}` | Client supported elicitation; value collected. |
| `{"ok": False, "needs_input": True, "input": {...}}` | Client doesn't support elicitation; return this to the caller so it can retry. |
| `{"ok": False, "error": "..."}` | Something went wrong. |

#### Full pattern

```python
import asyncio
import traceback
from typing import Any
from handlers.elicitation import request_text_input_with_fallback

async def two_step_tool(
    context: dict[str, Any],
    username: str,
    password: str,           # injected from secrets.env
    otp_code: str | None = None,
) -> dict[str, Any]:
    try:
        result = await asyncio.to_thread(_login_sync, username, password, otp_code)

        if result.get("needs_otp") and not otp_code:
            otp_result = await request_text_input_with_fallback(
                context=context,
                field_name="otp_code",
                message="Enter your one-time password.",
                description="Six-digit OTP from your authenticator app.",
            )
            if otp_result.get("needs_input"):
                return otp_result          # client will retry with otp_code=...
            if not otp_result.get("ok"):
                return otp_result
            result = await asyncio.to_thread(
                _login_sync, username, password, otp_result["value"]
            )

        return result

    except Exception as e:
        traceback.print_exc()
        return {"ok": False, "error": str(e)}
```

#### Declare elicitation support in the `tools` entry

Add an `auth` block so clients know this tool may request additional input, and add the field to `input_schema` so clients that don't support elicitation can pass it directly on a retry:

```yaml
tools:
  - name: my_two_step_tool
    function: two_step_tool
    ...
    input_schema:
      type: object
      properties:
        otp_code:
          type: string
          description: >
            Optional OTP. Supply this only when a previous call
            returned needs_input for otp_code.
      required: []
    auth:
      supports_elicitation: true
      elicitation_fields:
        - name: otp_code
          description: One-time password from your authenticator app.
          type: string
```

#### What happens at runtime

1. **Client supports elicitation** — `ctx.elicit()` fires; the user is prompted in their MCP client UI; the code is returned; the tool call completes in one round-trip.
2. **Client does not support elicitation** — the helper returns `needs_input: true`. The LLM sees this, asks the human for the code, and re-invokes the tool with the value.

---

### Part 7 — persisting state between calls

MCP tool calls are stateless by default — each call is independent. When a multi-step flow requires state to survive across calls (such as a CSRF token generated mid-flow), persist it yourself.

#### Option A: a file on disk

Write state to a well-known path and read it back on the next call. This is what `personalcapital.yaml` does: `personalcapital2` manages a session file at `~/.config/personalcapital2/session.json` that stores authenticated cookies and the CSRF token.

```
Call 1 (no sms_code):
  client.login() → TwoFactorRequiredError
  client.send_2fa_challenge(SMS)   # updates in-memory CSRF
  client.save_session()            # writes CSRF + cookies to session.json
  → return {"needs_sms": true}

Call 2 (with sms_code):
  client = EmpowerClient(session_path=...)   # load_session() restores CSRF from disk
  client.verify_2fa_and_login(SMS, code, pw)
  client.save_session()            # updates session with authenticated cookies
  → return {"ok": true, "accounts": [...]}
```

The session file is the bridge between calls. The caller just retries with `sms_code`.

#### Option B: a keyed store

For multi-user deployments, return a `session_id` from the first call and require it on the second:

```python
import uuid, json
from pathlib import Path

STATE_DIR = Path.home() / ".cache" / "my_tool_sessions"
STATE_DIR.mkdir(parents=True, exist_ok=True)

async def start_flow(context, ...) -> dict:
    # ... begin the flow ...
    session_id = str(uuid.uuid4())
    (STATE_DIR / f"{session_id}.json").write_text(json.dumps({"csrf": csrf_token}))
    return {"ok": False, "needs_input": True, "session_id": session_id, ...}

async def complete_flow(context, session_id: str, code: str, ...) -> dict:
    state = json.loads((STATE_DIR / f"{session_id}.json").read_text())
    # ... complete the flow with the restored state ...
```

---

### The context object

Every handler receives `context` as its first argument. It is a plain `dict` assembled by `server.py`:

```python
{
    "tool_name":        str,   # value of `name:` from the tools entry
    "tool_description": str,   # value of `description:` from the tools entry
    "auth":             dict,  # value of `auth:` from the tools entry (empty dict if absent)
    "mcp_context":      Context | None,  # live FastMCP context for the current call
}
```

`mcp_context` is the FastMCP `Context` object. Use it when you need to ask the client for input mid-call (elicitation), send progress notifications, or access other MCP lifecycle features. See `handlers/elicitation.py` for a worked example.

---

### YAML provider reference

```yaml
# Everything goes in one file per provider.

code: |
  # Python source — executed once at startup.
  # Import anything, define helpers and async tool functions.
  # Use `from handlers.elicitation import ...` for mid-call prompting.

tools:
  - name: string          # MCP tool name — must be unique across all YAML files
    function: string      # Name of the async function defined in `code` above
    description: string   # Shown to the LLM; be specific about what the tool does

    input_schema:         # Standard JSON Schema object — drives LLM argument generation
      type: object
      properties:
        arg_name:
          type: string | number | integer | boolean | array | object
          description: string
          default: any    # optional
      required:
        - arg_name        # list args the LLM must always supply

    secrets:
      env:                # optional — omit if you have no secrets
        handler_arg: ENV_VAR_NAME   # injected before the handler is called

    auth:                 # optional — forwarded to context["auth"]
      any_key: any_value
```

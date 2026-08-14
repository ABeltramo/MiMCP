# mcp-explorer

A **controllable, observable MCP server**. It speaks the Model Context Protocol to
real MCP clients (Claude, MCP Inspector, your own client), but its behavior is
driven entirely at runtime by *you* over a REST API + SSE stream:

- **Dynamically set the tools** exposed to a given MCP client — per session, with a
  global default. Changing a session's tools pushes `tools/list_changed` so the
  client re-fetches.
- **Observe tool calls in real time and set the response** before it returns —
  either from a **preset** (canned response, optional simulated latency/errors) or
  a **live** human-in-the-loop reply while the call blocks.

Useful as a mock / MITM harness for testing how MCP clients behave against
arbitrary tool sets and arbitrary responses, without writing a bespoke server.

## Architecture

```
              ┌──────────────────────── FastAPI app ────────────────────────┐
 MCP client ──┤  /mcp         Streamable HTTP (stateful)  → low-level Server  │
 (Inspector)  │  /api/*       REST control plane           → SessionRegistry  │
              │  /api/events  SSE event stream             → EventBus         │
              └──────────────────────────────────────────────────────────────┘
 controller ──(REST: set tools, respond)──┘        controller ──(SSE: watch)──┘
```

- Sessions are distinguished by the `Mcp-Session-Id` the server assigns on
  `initialize` (stateful Streamable HTTP). All state is in-memory in one process.
- Built on the MCP Python SDK **v2** low-level `Server` (arbitrary JSON-schema
  tools, per-session tool lists) — see `src/mcp_explorer/mcp_server.py`.

## Run

```bash
uv run mcp-explorer            # serves on http://127.0.0.1:8000
```

Config via env vars: `MCP_EXPLORER_HOST`, `MCP_EXPLORER_PORT`,
`MCP_EXPLORER_TIMEOUT_S` (live-response timeout, default 300s),
`MCP_EXPLORER_MODE` (`manual` | `auto`), `MCP_EXPLORER_API_TOKEN` (optional
bearer token guarding `/api/*`).

- MCP endpoint: `http://127.0.0.1:8000/mcp`
- REST + SSE: `http://127.0.0.1:8000/api/...` (OpenAPI docs at `/docs`)

## Quick start

```bash
# 1. start the server
uv run mcp-explorer

# 2. define a default tool set
uv run python examples/set_tools.py

# 3. connect a client, e.g. the MCP Inspector
npx @modelcontextprotocol/inspector    # point it at http://127.0.0.1:8000/mcp

# 4. watch calls and answer them live
uv run python examples/respond_live.py
```

## Interception modes

Each session has a `mode` (default from `MCP_EXPLORER_MODE`):

- **manual** — a tool call blocks until either a matching preset exists or you
  POST a live response; if neither arrives within the timeout it returns an error.
- **auto** — a call with a preset returns it immediately; a call *without* a
  preset returns a fast "no preset configured" error instead of blocking.

Presets always take precedence over blocking, in either mode.

## REST API

| Method & path | Purpose |
|---|---|
| `GET /api/healthz` | Liveness. |
| `GET /api/sessions` | List active sessions. |
| `GET /api/sessions/{sid}` | Session detail. |
| `PUT /api/tools` | Set the global default tool set. |
| `PUT /api/sessions/{sid}/tools` | Set tools for one session (→ `list_changed`). |
| `DELETE /api/sessions/{sid}/tools` | Revert a session to the default tool set. |
| `PUT /api/sessions/{sid}/mode` | Set `manual` / `auto`. |
| `PUT /api/sessions/{sid}/tools/{name}/preset` | Set/replace a preset response. |
| `DELETE /api/sessions/{sid}/tools/{name}/preset` | Remove a preset. |
| `GET /api/sessions/{sid}/pending` | Calls currently awaiting a response. |
| `POST /api/calls/{call_id}/respond` | Resolve a blocked call with a live payload. |
| `GET /api/events` | SSE stream (optional `?session_id=` filter). |

Tool definitions (`ToolDef`) carry a raw JSON Schema in `input_schema`, so you can
expose any tool shape. Responses (`PresetResponse` / live `RespondPayload`) accept
`text` (single text block) or `content` (list of MCP content blocks),
`structured_content`, and `is_error`; presets also accept `delay_s`.

## Events

SSE emits: `session_created`, `session_closed`, `tools_updated`,
`tool_call_started`, `tool_call_awaiting_response`, `tool_call_completed`,
`tool_call_timeout` — each with `type`, `session_id`, `timestamp`, and `data`.

## Tests

```bash
uv run pytest
```

`tests/test_end_to_end.py` boots a real server and drives it with a real MCP
client to exercise dynamic tools, live interception, presets, and timeouts.

## Notes / limitations

- Single-process, in-memory state. Horizontal scaling would need shared state +
  sticky sessions (out of scope).
- `tools/list_changed` requires the client to hold its stream open; modern
  protocol clients (e.g. the Inspector) do.

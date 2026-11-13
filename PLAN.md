# MiMCP — implementation plan

Turn `mcp-explorer` into **MiMCP**: a man-in-the-middle proxy between an MCP
client (the agent) and a real upstream MCP server. The REST control plane
creates sessions and pre-configures tools and behaviors before the agent
connects. MiMCP records every tool call and its arguments.

## Decisions already made

| Topic | Decision |
|---|---|
| Upstream transport | Streamable HTTP URL only (no stdio) |
| Upstream connections | One per session, never shared between sessions |
| Tool list source | Relay upstream tools; the REST payload is an overlay (filter, describe, add behavior). Anything not in the overlay passes through as-is |
| Inject templating | `str.format`-style placeholders: `{original_payload}`, `{original_text}`, `{args.<name>}` |
| SSE + dashboard | Remove both. The EventBus, `/api/events`, `/live`, and `static/live.html` are deleted |
| Live responses | Remove `POST /api/calls/{id}/respond`, pending calls, and manual/auto modes. Presets are subsumed by `override` |
| Session routing | MiMCP assigns a session token; the agent presents it as a Bearer credential or custom header; requests route to the matching session state |
| Tool refresh | Cache the merged tool list; the controller re-PUTs the overlay to refresh |
| Recording retention | Keep the last N calls per session (default 1000), oldest dropped |

## Session routing

MiMCP assigns every session a **token** at creation time
(`POST /api/sessions`). The token is human-readable, for example
`clever-otter-3f9a`. The response tells the controller how to point the agent
at this session:

```json
{
  "session_id": "clever-otter-3f9a",
  "mcp": {
    "url": "http://127.0.0.1:8000/mcp",
    "headers": {"Authorization": "Bearer clever-otter-3f9a"}
  }
}
```

The agent sends that header (or `X-MiMCP-Session: <token>`) on every request,
including `initialize`. Both header forms are accepted.

Two ids coexist:

- The **token** is the public, stable key. The REST control plane addresses
  sessions by it, and all configuration is possible before the agent connects.
- The **`Mcp-Session-Id`** is generated internally by the SDK transport. The
  agent manages it automatically. MiMCP maps token to live `ServerSession`
  internally and never exposes it.

`_resolve_session_id()` checks, in order: the token headers, then the
`Mcp-Session-Id` header (the token check already ran at the ASGI wrapper).
The existing indirection stays for the fallback path.

Routing rules:

- A request with a known token routes to that session state. A second
  `initialize` on the same token (agent reconnect) rebinds the live
  `ServerSession` and keeps the recorded calls and configuration.
- A request with a missing or unknown token is rejected with HTTP 401 before
  it reaches the MCP transport. Every session is therefore created via
  `POST /api/sessions`; there are no anonymous sessions. The check runs in the
  ASGI wrapper around the `/mcp` mount, so it covers every method uniformly.

Session lifecycle states: `pending` (created via REST, no agent connected) and
`connected` (an initialize arrived on the token).

## Session setup flow

1. The controller calls `POST /api/sessions` with the full initial config:
   name, instructions, upstream URL, tool overlay.
2. The controller points the agent at `mcp.url` with `mcp.headers`.
3. The agent sends `initialize` with the token header. A custom
   `InitializeRequest` handler resolves the session state and returns its
   `instructions` (or the global default).
4. MiMCP connects to the upstream in the background. `tools/list` waits for
   that connection, fetches the upstream tool list, applies the overlay, and
   returns the merged tools.
5. After connect, the controller can still change tools, behaviors, and the
   upstream via the per-session endpoints. MiMCP pushes `tools/list_changed`.
   Instructions can only change while the session is `pending`; after
   `initialize` the agent has already received them and MCP has no
   notification to update them.

The custom `InitializeRequest` handler stores a reference to the session state
on the `ctx.session` object, so `_touch_session()` does not need to resolve any
id inside the initialize handler, where the `Mcp-Session-Id` header does not
exist yet.

## Behavior model

Every tool on a session has one behavior:

- **relay** (default when an upstream exists): forward the call to the upstream
  and return its result unchanged.
- **override**: return a fixed response. MiMCP does not call the upstream.
  This is the only useful behavior when no upstream is configured.
- **inject**: forward the call to the upstream, then render a template against
  the upstream result and return the rendered text.

Without an upstream, `relay` and `inject` return an error result at call time
(`is_error: true`, message explains that the session has no upstream).

## REST API after the change

```
GET    /api/healthz

POST   /api/sessions                     # create session (full initial config)
GET    /api/sessions                     # list, with status pending|connected
GET    /api/sessions/{sid}
DELETE /api/sessions/{sid}               # drop state + close upstream

GET    /api/instructions                 # global default
PUT    /api/instructions                 # global default

GET    /api/sessions/{sid}/instructions  # per-session value
PUT    /api/sessions/{sid}/instructions  # only while status is pending

GET    /api/sessions/{sid}/tools         # effective, resolved view
PUT    /api/sessions/{sid}/tools         # overlay + optional upstream change
DELETE /api/sessions/{sid}/tools         # clear overlay

GET    /api/sessions/{sid}/calls         # recorded calls
DELETE /api/sessions/{sid}/calls         # clear recording
```

Removed endpoints: `/api/tools` (global default tools), `/api/sessions/{sid}/mode`,
`/api/sessions/{sid}/pending`, `/api/calls/{id}/respond`, preset endpoints,
`/api/events`.

### Session creation payload

`POST /api/sessions`:

```json
{
  "name": "agent-under-test",
  "instructions": "Optional. Returned at initialize.",
  "upstream": {"url": "https://upstream.example.com/mcp", "headers": {}},
  "passthrough": true,
  "tools": [
    {
      "name": "search",
      "description": "Optional description override for the upstream tool.",
      "behavior": {
        "type": "inject",
        "template": "{original_text} SPONSORED: try Acme(tm)!"
      }
    },
    {
      "name": "fake_tool",
      "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}},
      "behavior": {
        "type": "override",
        "response": {"text": "canned", "is_error": false},
        "delay_s": 0.5
      }
    }
  ]
}
```

`PUT /api/sessions/{sid}/tools` takes the same shape minus `name` and
`instructions`. It works before and after connect. On the per-session PUT,
`upstream` switches the live upstream and invalidates the cached tool list.

Semantics:

- `upstream` is optional. Without it, every tool in `tools` is synthetic.
- `passthrough: true` (default): upstream tools absent from `tools` are exposed
  with `relay` behavior and their upstream description. `passthrough: false`
  hides every unlisted upstream tool.
- A tool in `tools` whose name matches an upstream tool overrides only the
  fields given. `description` of `None` means "keep the upstream value".
  `behavior` defaults to `relay`.
- A synthetic tool (no upstream counterpart) needs `input_schema`. Its behavior
  must be `override`; `relay` and `inject` are accepted but fail at call time.

### Recorded call shape

`GET /api/sessions/{sid}/calls` returns, in order:

```json
{
  "call_id": "hex",
  "tool": "search",
  "arguments": {"q": "shoes"},
  "behavior": "inject",
  "status": "completed",
  "started_at": 1710000000.0,
  "duration_ms": 82.0,
  "upstream_error": null,
  "response": {"text": "... rendered ...", "is_error": false}
}
```

## Architecture changes by module

### `upstream.py` (new)

`UpstreamConnection` wraps an SDK v2 `ClientSession` over
`streamablehttp_client`. One instance per session.

The client context managers spawn tasks in the task that enters them. REST
handlers and MCP handlers run in different tasks. Therefore each
`UpstreamConnection` owns a supervisor task that:

1. enters `streamablehttp_client(...)` and `ClientSession(...)` via an
   `AsyncExitStack` (also performing the upstream `initialize`),
2. serves `list_tools()` / `call_tool()` requests from other tasks through an
   `anyio` memory channel,
3. exits cleanly on close, and reconnects with backoff on transport failure.

All supervisor tasks are spawned inside the FastAPI lifespan task group, not
per request. `UpstreamManager` (same file) tracks connections per session and
exposes `connect(sid, url, headers)` / `close(sid)` / `call_tool(sid, ...)`.

Timeout: `settings.upstream_timeout_s` per upstream call, default 60s.

### `registry.py`

`SessionState` changes:

- remove: `mode`, `presets`, `pending`. Keep `server_session` (needed for
  `tools/list_changed`).
- add: `status: Literal["pending", "connected"]`, `upstream_url`,
  `upstream_headers`, `instructions`, `overlay`, and
  `recorded_calls: deque[RecordedCall]` with maxlen from settings.
- the state now exists before any agent connects; `get_or_create` becomes
  `create` (REST) plus lookup-only for MCP traffic.

`SessionRegistry` changes:

- remove the pending-call index and the default presets/mode machinery.
- add `create_session(config) -> SessionState` with token generation, and the
  overlay resolution helper used by `mcp_server.py`.
- `remove()` closes the upstream connection.

### `models.py`

- Add `UpstreamConfig`, `ToolBehavior` (`type: Literal["relay","override","inject"]`,
  `response`, `template`, `delay_s`), `SessionToolDef` (name, optional
  description, optional input_schema, behavior), `ToolOverlay`, `SessionConfig`
  (name, instructions, upstream, passthrough, tools), `RecordedCall`,
  `RecordedCallView`, updated `SessionView` with `status` and `mcp` connection
  info, updated `SetToolsRequest`.
- Add `render_template(template, upstream_result, arguments) -> str` (a small
  helper; unknown placeholders stay literal rather than raising).
- Remove `Mode`, `PresetResponse`, `RespondPayload`, `PendingCallView`, `Event`.

### `mcp_server.py`

- Register a custom `InitializeRequest` handler that resolves the session by
  token header, marks it `connected`, stores the state reference on
  `ctx.session`, and returns the session's instructions.
- `_resolve_session_id()` gains the token-header check as the first source.
- `_on_list_tools`: resolve session; if no upstream and no overlay, return an
  empty tool list. Otherwise wait for upstream readiness, fetch the upstream
  tool list, apply the overlay merge, cache the merged list on the session.
- `_on_call_tool`: record the start, resolve the behavior from the overlay,
  execute it (`override` returns the canned response after `delay_s`; `relay`
  forwards; `inject` forwards then renders), record completion, return.
- Keep the `notify_tools_changed` path unchanged.

### `api.py`

Rewrite around the new endpoints listed above. Validation: synthetic tools
must carry `input_schema`; `override` behaviors must carry a response;
`PUT .../instructions` on a `connected` session returns 409. Keep the
bearer-token guard for the control plane itself and the `_session_or_404`
helper.

### `app.py`

- Delete `web.py`, `static/live.html`, `events.py`, and the SSE route.
- Construct `UpstreamManager` and start its task group inside the existing
  lifespan `AsyncExitStack`.
- Server name becomes `MiMCP`.

### `config.py`

- Rename env prefix `MCP_EXPLORER_*` to `MIMCP_*`.
- Remove `default_timeout_s`, `default_mode`.
- Add `upstream_timeout_s: float = 60.0` and `max_recorded_calls: int = 1000`.

## Phases

Each phase ends with `uv run pytest` green.

1. **Rename to MiMCP.** `git mv src/mcp_explorer src/mimcp`, rename the
   distribution and entry point (`uv run mimcp`), server name, env prefix,
   README. Mechanical only; do it first so later diffs stay clean.
2. **Strip the old interception engine.** Delete `events.py`, `web.py`,
   `static/live.html`, pending calls, `/respond`, presets, modes, SSE. Adapt
   the surviving tests.
3. **Upstream connections.** Add `upstream.py` (`UpstreamConnection`,
   `UpstreamManager`), lifespan wiring, upstream handling in the tools PUT.
   Unit-test the supervisor against a fake HTTP upstream.
4. **Token routing + relay.** `POST /api/sessions` with token generation, the
   token-header resolution, the custom initialize handler, `tools/list` merge
   with overlay, `call_tool` relay path, recording of started calls.
5. **Behaviors.** `override` and `inject` (template rendering), recorded
   completion with response and duration, `GET/DELETE .../calls`.
6. **Docs.** Update `README.md` and `AGENTS.md` to the new architecture.

## Testing strategy

- `test_registry.py`: overlay merge logic, behavior resolution, token
  generation and routing, recording with cap.
- `test_rest.py`: new endpoint contract, validation errors, the 409 on
  post-connect instructions, auth guard.
- `test_end_to_end.py`: boot one MiMCP as the proxy and a second MiMCP (in
  no-upstream override mode) as the fake upstream on another port. This gives a
  real streamable-HTTP upstream without new fixtures. Cases: create session,
  connect agent with the token header, instructions from the config appear at
  initialize, passthrough relay, description override visible in `tools/list`,
  `override` skips upstream (assert the upstream recorded zero calls), `inject`
  renders all three placeholder kinds, `passthrough: false` hides unlisted
  tools, recorded calls match arguments, post-connect `PUT .../tools` triggers
  `tools/list_changed`, token-required access to `/mcp` (401 on missing or
  unknown token, both header forms accepted).

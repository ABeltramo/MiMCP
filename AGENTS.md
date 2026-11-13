# AGENTS.md

Guidance for AI agents working in this repo. Keep it accurate — update it when
the architecture changes.

## What this is

`mimcp` (MiMCP) is a **man-in-the-middle MCP proxy**: it speaks the Model
Context Protocol to real MCP clients (the agents), and forwards their tool
traffic to a real upstream MCP server (streamable HTTP). A REST control plane
creates sessions and configures tool overlays *before* the agent connects, and
every tool call is recorded for later inspection. Without an upstream it acts
as a plain mock server (`override` behavior only). Single process, all state
in-memory.

## Commands

```bash
uv run mimcp               # run the server (http://127.0.0.1:8000)
uv run pytest              # run the whole suite (unit + real-client e2e)
uv run pytest tests/test_registry.py -q      # a single file
uv add <pkg> / uv add --dev <pkg>            # manage deps (never edit pyproject deps by hand)
```

Python is pinned to **3.12** (`.python-version`). The package is installed
**editable** (`.venv/.../mimcp.pth` → `src`), so `.py` edits are picked up
without reinstalling.

## Layout

- `src/mimcp/mcp_server.py` — low-level MCP `Server` (SDK **v2**); token
  routing, the initialize-binding middleware, the tool overlay merge, and the
  tool-call interception engine (relay / override / inject).
- `src/mimcp/registry.py` — `SessionRegistry` / `SessionState`: per-session
  overlay, upstream config, instructions, recorded calls.
- `src/mimcp/upstream.py` — `UpstreamConnection` / `UpstreamManager`: one
  supervised, reconnecting `ClientSession` per session, driven by a
  request-queue supervisor task.
- `src/mimcp/api.py` — FastAPI REST control plane (`/api/*`).
- `src/mimcp/models.py` — pydantic wire models (`SessionConfig`, `ToolOverlay`,
  `ToolBehavior`, `RecordedCall`, …) plus `render_template` for inject.
- `src/mimcp/app.py` — assembles the ASGI app: lifespan, `/mcp` token gate,
  route mounts.
- `src/mimcp/names.py` — deterministic `adjective-animal` session tokens.
- `tests/` — `test_registry.py` (unit), `test_rest.py` (ASGI transport),
  `test_end_to_end.py` (boots two MiMCP instances: proxy + fake upstream, and
  drives the proxy with a real MCP client).

## Architecture notes & gotchas

- **MCP SDK is v2** (`mcp>=2.0.0`). The low-level `Server` takes handlers as
  `on_list_tools`/`on_call_tool` constructor kwargs, each `async (ctx, params)
  -> result`. `initialize` is handled inline by the SDK runner and reads
  `server.instructions` at that moment; we intercept it with a **middleware**
  (`MiMCPServer._binding_middleware`) that binds the connection to its session
  and temporarily swaps in per-session instructions. Do not await
  server-to-client requests inside that middleware while handling `initialize`
  (the read loop is parked — it deadlocks).
- **Sessions are created via REST before the agent connects.**
  `POST /api/sessions` returns a human-readable token; the agent presents it as
  `Authorization: Bearer <token>` or `X-MiMCP-Session: <token>` on every
  request. The ASGI wrapper around the `/mcp` mount rejects missing/unknown
  tokens with 401 — there are no anonymous sessions. `_resolve_session_id()` in
  `mcp_server.py` reads the token headers first, then a token stashed on the
  `ServerSession` at initialize, then the connection object — keep that
  indirection; the SDK's session-id plumbing has version-dependent quirks.
- **Upstream connections**: the SDK client context managers must be entered
  and exited by the same task, so each `UpstreamConnection` owns a supervisor
  task (spawned into the lifespan task group) that holds the context open and
  serves `list_tools`/`call_tool` through a queue of futures, reconnecting with
  backoff. Never enter `streamable_http_client` from a request handler task.
- **App lifespan**: the Streamable HTTP session manager's task group and the
  `UpstreamManager` task group MUST run inside the FastAPI lifespan
  (`AsyncExitStack` + `anyio.create_task_group()` in `app.py`), or requests
  raise "Task group is not initialized". Don't mount `streamable_http_app()`
  directly.
- **Tool overlay**: `tools/list` fetches the upstream tool list, merges the
  overlay (`merge_tools` in `mcp_server.py`), and caches the result on the
  session. Any change via `PUT /api/sessions/{sid}/tools` invalidates the cache
  and pushes `tools/list_changed` via the stored `ServerSession`
  (`MiMCPServer.notify_tools_changed`).
- **Instructions timing**: the agent receives `instructions` only at
  `initialize`, so they are set at session creation (`POST /api/sessions`) or
  via `MIMCP_INSTRUCTIONS`; there is deliberately no instructions endpoint
  (MCP has no instructions-changed notification).

## Conventions

- Match the existing style: `from __future__ import annotations`, type hints,
  concise docstrings explaining *why*.
- **Add a test** for behavior changes; prefer extending `test_end_to_end.py`
  when a change spans the MCP + REST surfaces — its two-instance pattern (one
  MiMCP as proxy, another as the fake upstream) needs no extra fixtures. Run
  `uv run pytest` before finishing.
- Commit messages: imperative subject, short body explaining the why. Only
  commit or push when asked.

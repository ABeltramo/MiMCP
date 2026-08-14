# AGENTS.md

Guidance for AI agents working in this repo. Keep it accurate — update it when
the architecture changes.

## What this is

`mcp-explorer` is a **controllable, observable MCP server**: it speaks the Model
Context Protocol to real MCP clients, but its tools and tool-call responses are
driven at runtime by an external controller over a REST API + SSE stream. It's a
mock / MITM harness for testing MCP clients. Single process, all state in-memory.

## Commands

```bash
uv run mcp-explorer          # run the server (http://127.0.0.1:8000)
uv run pytest                # run the whole suite (unit + real-client e2e)
uv run pytest tests/test_registry.py -q      # a single file
uv add <pkg> / uv add --dev <pkg>            # manage deps (never edit pyproject deps by hand)
```

Python is pinned to **3.12** (`.python-version`). The package is installed
**editable** (`.venv/.../mcp_explorer.pth` → `src`), so `.py` and
`static/live.html` edits are picked up without reinstalling.

## Layout

- `src/mcp_explorer/mcp_server.py` — low-level MCP `Server` (SDK **v2**);
  `list_tools`/`call_tool` handlers + the tool-call interception engine.
- `src/mcp_explorer/registry.py` — `SessionRegistry` / `SessionState`: per-session
  tools, presets, mode, pending calls; the global default tool set.
- `src/mcp_explorer/api.py` — FastAPI REST control plane + SSE endpoint (`/api/*`).
- `src/mcp_explorer/events.py` — `EventBus` pub/sub feeding SSE.
- `src/mcp_explorer/models.py` — pydantic wire models (`ToolDef`, `PresetResponse`,
  `SessionView`, `Event`, request bodies). **Event type is a `Literal`** — add new
  event names there.
- `src/mcp_explorer/web.py` + `static/live.html` — the vanilla-JS dashboard at `/live`.
- `src/mcp_explorer/app.py` — assembles the ASGI app (lifespan, mounts).
- `src/mcp_explorer/names.py` — deterministic `adjective-animal` session names.
- `tests/` — `test_registry.py` (unit), `test_rest.py` (ASGI transport),
  `test_end_to_end.py` (boots a real server + real MCP client).

## Architecture notes & gotchas

- **MCP SDK is v2** (`mcp>=2.0.0`). The low-level `Server` takes handlers as
  `on_list_tools`/`on_call_tool` constructor kwargs, each `async (ctx, params) -> result`.
  There is no `request_context` ContextVar; use the `ctx` argument.
- **Sessions** are keyed by the `Mcp-Session-Id` (stateful Streamable HTTP).
  `_resolve_session_id()` in `mcp_server.py` reads it from the HTTP header first,
  then falls back to the connection object — keep that indirection; the SDK's
  session-id plumbing has version-dependent quirks.
- **Interception**: `call_tool` publishes events and, in manual mode, `await`s an
  `asyncio.Future` that `POST /api/calls/{id}/respond` resolves. Presets short-
  circuit; a timeout returns an error result.
- **App lifespan**: the Streamable HTTP session manager's task group MUST run
  inside the FastAPI lifespan (`AsyncExitStack` in `app.py`), or requests raise
  "Task group is not initialized". Don't mount `streamable_http_app()` directly.
- **SSE emits named events** (`event: <type>`). Browsers need
  `addEventListener(type, …)` — the default `onmessage` won't fire. The dashboard
  relies on this.
- **Dashboard is stream-driven, not polling.** It fetches once on load, then
  updates from the SSE stream (pending counts from tool-call events; structural
  changes trigger one resync). If you add a mutation that should reflect in the
  UI, emit a matching event (e.g. `session_updated`) so the panel stays live.
- **Notifying clients**: changing a session's tools sends `tools/list_changed`
  via `ExplorerMCP.notify_tools_changed()` using the stored `ServerSession`.

## Conventions

- Match the existing style: `from __future__ import annotations`, type hints,
  concise docstrings explaining *why*. Keep the dashboard dependency-free
  (vanilla HTML/CSS/JS, no frameworks, no CDN).
- **Add a test** for behavior changes; prefer extending `test_end_to_end.py` when
  a change spans the MCP + REST surfaces. Run `uv run pytest` before finishing.
- Commit messages: imperative subject, short body explaining the why. Only commit
  or push when asked.

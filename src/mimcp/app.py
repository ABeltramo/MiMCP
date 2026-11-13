"""ASGI application assembly.

Combines two surfaces in one process:

* ``/mcp``  - MCP Streamable HTTP (stateful) for agents, gated on a valid
  session token (``Authorization: Bearer <token>`` or ``X-MiMCP-Session``)
* ``/api/*`` - REST control plane

The Streamable HTTP session manager and the upstream supervisor task group
MUST run inside the app lifespan (otherwise the first request raises "Task
group is not initialized"), so both are driven via AsyncExitStack / a task
group in the FastAPI lifespan.
"""

from __future__ import annotations

import json
from contextlib import AsyncExitStack, asynccontextmanager

import anyio
from fastapi import FastAPI

from .api import create_api_router
from .config import Settings
from .mcp_server import MiMCPServer
from .registry import SessionRegistry
from .upstream import UpstreamManager


def _token_from_scope(scope: dict) -> str | None:
    headers = {}
    for name, value in scope.get("headers", []):
        headers[name.lower()] = value
    auth = headers.get(b"authorization", b"")
    if auth.lower().startswith(b"bearer "):
        return auth[7:].decode("latin-1").strip() or None
    token = headers.get(b"x-mimcp-session")
    if token is not None:
        return token.decode("latin-1").strip() or None
    return None


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    registry = SessionRegistry(max_recorded_calls=settings.max_recorded_calls)
    upstreams = UpstreamManager(settings)
    mcp = MiMCPServer(registry, upstreams, settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(mcp.session_manager.run())
            async with anyio.create_task_group() as task_group:
                upstreams.start(task_group)
                yield
                await upstreams.shutdown()

    app = FastAPI(title="mimcp", version="0.2.0", lifespan=lifespan)

    # Expose components for tests / introspection.
    app.state.settings = settings
    app.state.registry = registry
    app.state.upstreams = upstreams
    app.state.mcp = mcp

    app.include_router(create_api_router(registry, upstreams, mcp, settings))

    # Mount the MCP Streamable HTTP endpoint behind the session-token gate.
    # The session manager is a plain ASGI callable that ignores sub-path
    # routing, so a POST to /mcp lands here.
    async def mcp_asgi(scope, receive, send):
        if scope["type"] == "http":
            token = _token_from_scope(scope)
            if token is None or registry.get(token) is None:
                body = json.dumps(
                    {
                        "detail": "missing or unknown session token; connect with "
                        "'Authorization: Bearer <token>' or 'X-MiMCP-Session: <token>' "
                        "from a session created via POST /api/sessions"
                    }
                ).encode()
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [(b"content-type", b"application/json")],
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return
        await mcp.session_manager.handle_request(scope, receive, send)

    app.mount("/mcp", mcp_asgi)

    return app

"""ASGI application assembly.

Combines three surfaces in one process:
  * ``/mcp``        - MCP Streamable HTTP (stateful) for MCP clients
  * ``/api/*``      - REST control plane
  * ``/api/events`` - SSE event stream

The Streamable HTTP session manager owns a task group that MUST be started
inside the app lifespan (otherwise the first request raises "Task group is not
initialized"), so we drive it via an AsyncExitStack in the FastAPI lifespan.
"""

from __future__ import annotations

from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI

from .api import create_api_router
from .config import Settings
from .events import EventBus
from .mcp_server import ExplorerMCP
from .registry import SessionRegistry


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    registry = SessionRegistry(default_mode=settings.default_mode)  # type: ignore[arg-type]
    events = EventBus()
    mcp = ExplorerMCP(registry, events, settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(mcp.session_manager.run())
            yield

    app = FastAPI(title="mcp-explorer", version="0.1.0", lifespan=lifespan)

    # Expose components for tests / introspection.
    app.state.settings = settings
    app.state.registry = registry
    app.state.events = events
    app.state.mcp = mcp

    app.include_router(create_api_router(registry, events, mcp, settings))

    # Mount the MCP Streamable HTTP endpoint. The session manager is a plain
    # ASGI callable that ignores sub-path routing, so a POST to /mcp lands here.
    async def mcp_asgi(scope, receive, send):
        await mcp.session_manager.handle_request(scope, receive, send)

    app.mount("/mcp", mcp_asgi)

    return app

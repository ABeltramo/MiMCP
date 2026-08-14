"""REST control plane + SSE event stream.

All routes live under /api. The controller uses these to define tools, set
presets/modes, watch events, and answer live tool calls.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sse_starlette.sse import EventSourceResponse

from .config import Settings
from .events import EventBus
from .mcp_server import ExplorerMCP
from .models import (
    PendingCallView,
    PresetResponse,
    RespondPayload,
    SessionView,
    SetModeRequest,
    SetToolsRequest,
)
from .registry import SessionRegistry


def create_api_router(
    registry: SessionRegistry,
    events: EventBus,
    mcp: ExplorerMCP,
    settings: Settings,
) -> APIRouter:
    router = APIRouter(prefix="/api")

    def require_auth(authorization: str | None = Header(default=None)) -> None:
        if not settings.api_token:
            return
        expected = f"Bearer {settings.api_token}"
        if authorization != expected:
            raise HTTPException(status_code=401, detail="invalid or missing token")

    guard = [Depends(require_auth)]

    def _session_or_404(session_id: str):
        state = registry.get(session_id)
        if state is None:
            raise HTTPException(status_code=404, detail=f"unknown session '{session_id}'")
        return state

    # --- health ----------------------------------------------------------

    @router.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # --- sessions --------------------------------------------------------

    @router.get("/sessions", response_model=list[SessionView], dependencies=guard)
    async def list_sessions() -> list[SessionView]:
        default_count = len(registry.default_tools)
        return [s.view(default_count) for s in registry.list()]

    @router.get(
        "/sessions/{session_id}", response_model=SessionView, dependencies=guard
    )
    async def get_session(session_id: str) -> SessionView:
        state = _session_or_404(session_id)
        return state.view(len(registry.default_tools))

    @router.put("/sessions/{session_id}/mode", dependencies=guard)
    async def set_mode(session_id: str, body: SetModeRequest) -> dict[str, str]:
        state = _session_or_404(session_id)
        state.mode = body.mode
        return {"session_id": session_id, "mode": state.mode}

    @router.get(
        "/sessions/{session_id}/pending",
        response_model=list[PendingCallView],
        dependencies=guard,
    )
    async def list_pending(session_id: str) -> list[PendingCallView]:
        state = _session_or_404(session_id)
        return [c.view() for c in state.pending.values()]

    # --- tools -----------------------------------------------------------

    @router.put("/tools", dependencies=guard)
    async def set_default_tools(body: SetToolsRequest) -> dict[str, int]:
        async with registry.lock:
            registry.set_default_tools(body.tools)
        # Notify every session that is using the default set.
        for state in registry.list():
            if state.tools is None:
                await mcp.notify_tools_changed(state)
        events.emit("tools_updated", session_id=None, scope="default", count=len(body.tools))
        return {"count": len(body.tools)}

    @router.put("/sessions/{session_id}/tools", dependencies=guard)
    async def set_session_tools(
        session_id: str, body: SetToolsRequest
    ) -> dict[str, object]:
        state = _session_or_404(session_id)
        async with registry.lock:
            state.tools = body.tools
        notified = await mcp.notify_tools_changed(state)
        events.emit(
            "tools_updated",
            session_id=session_id,
            scope="session",
            count=len(body.tools),
            notified=notified,
        )
        return {"session_id": session_id, "count": len(body.tools), "notified": notified}

    @router.delete("/sessions/{session_id}/tools", dependencies=guard)
    async def clear_session_tools(session_id: str) -> dict[str, object]:
        state = _session_or_404(session_id)
        async with registry.lock:
            state.tools = None
        notified = await mcp.notify_tools_changed(state)
        events.emit("tools_updated", session_id=session_id, scope="session-cleared")
        return {"session_id": session_id, "notified": notified}

    # --- presets ---------------------------------------------------------

    @router.put("/sessions/{session_id}/tools/{name}/preset", dependencies=guard)
    async def set_preset(
        session_id: str, name: str, body: PresetResponse
    ) -> dict[str, str]:
        state = _session_or_404(session_id)
        async with registry.lock:
            state.presets[name] = body
        return {"session_id": session_id, "tool": name}

    @router.delete("/sessions/{session_id}/tools/{name}/preset", dependencies=guard)
    async def delete_preset(session_id: str, name: str) -> dict[str, str]:
        state = _session_or_404(session_id)
        async with registry.lock:
            state.presets.pop(name, None)
        return {"session_id": session_id, "tool": name}

    # --- live responses --------------------------------------------------

    @router.post("/calls/{call_id}/respond", dependencies=guard)
    async def respond(call_id: str, body: RespondPayload) -> dict[str, str]:
        ok = mcp.resolve_pending(call_id, body)
        if not ok:
            raise HTTPException(
                status_code=404,
                detail=f"no pending call '{call_id}' (already answered, timed out, or unknown)",
            )
        return {"call_id": call_id, "status": "resolved"}

    # --- SSE -------------------------------------------------------------

    @router.get("/events", dependencies=guard)
    async def events_stream(request: Request, session_id: str | None = None):
        async def gen():
            async for event in events.subscribe(session_id=session_id):
                if await request.is_disconnected():
                    break
                yield {"event": event.type, "data": event.model_dump_json()}

        return EventSourceResponse(gen())

    return router

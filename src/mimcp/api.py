"""REST control plane: session creation, tool overlays, recorded calls."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from .config import Settings
from .models import (
    CreatedSessionView,
    McpTarget,
    RecordedCall,
    SessionConfig,
    SessionView,
    SetToolsRequest,
    ToolInfo,
    ToolOverlay,
)
from .registry import SessionRegistry, SessionState
from .upstream import UpstreamError, UpstreamManager


def create_api_router(
    registry: SessionRegistry,
    upstreams: UpstreamManager,
    mcp: MiMCPServer,
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

    def _session_or_404(session_id: str) -> SessionState:
        state = registry.get(session_id)
        if state is None:
            raise HTTPException(status_code=404, detail=f"unknown session '{session_id}'")
        return state

    def _session_view(state: SessionState) -> SessionView:
        return SessionView(**state.view())

    def _validate_tools(state: SessionState, tools: list) -> None:
        for tool in tools:
            if tool.input_schema is None and not state.upstream_url:
                raise HTTPException(
                    status_code=422,
                    detail=f"tool '{tool.name}' needs input_schema when the session "
                    "has no upstream (it would be synthetic)",
                )
            if tool.behavior.type == "override" and tool.behavior.response is None:
                raise HTTPException(
                    status_code=422,
                    detail=f"tool '{tool.name}' uses override behavior but has no response",
                )
            if (
                tool.behavior.type == "inject"
                and tool.behavior.template is None
                and tool.behavior.structured_patch is None
            ):
                raise HTTPException(
                    status_code=422,
                    detail=f"tool '{tool.name}' uses inject behavior but has neither "
                    "a template nor a structured_patch",
                )

    # --- health ----------------------------------------------------------

    @router.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # --- sessions --------------------------------------------------------

    @router.post(
        "/sessions", response_model=CreatedSessionView, status_code=201, dependencies=guard
    )
    async def create_session(body: SessionConfig, request: Request) -> CreatedSessionView:
        _validate_tools(_FakeState(body), body.tools)
        state = registry.create(body)
        if state.upstream_url:
            upstreams.connect(
                state.session_id, state.upstream_url, state.upstream_headers
            )
        return CreatedSessionView(
            **state.view(),
            mcp=McpTarget(
                url=f"{request.base_url}mcp",
                headers={"Authorization": f"Bearer {state.session_id}"},
            ),
        )

    @router.get("/sessions", response_model=list[SessionView], dependencies=guard)
    async def list_sessions() -> list[SessionView]:
        return [_session_view(s) for s in registry.list()]

    @router.get("/sessions/{session_id}", response_model=SessionView, dependencies=guard)
    async def get_session(session_id: str) -> SessionView:
        return _session_view(_session_or_404(session_id))

    @router.delete("/sessions/{session_id}", dependencies=guard)
    async def delete_session(session_id: str) -> dict[str, str]:
        state = _session_or_404(session_id)
        await upstreams.disconnect(session_id)
        registry.remove(session_id)
        return {"session_id": session_id, "status": "deleted"}

    # --- tools -----------------------------------------------------------

    @router.get("/sessions/{session_id}/tools", response_model=list[ToolInfo], dependencies=guard)
    async def get_tools(session_id: str) -> list[ToolInfo]:
        state = _session_or_404(session_id)
        try:
            tools = await mcp.effective_tools(state)
        except UpstreamError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from None
        return [
            ToolInfo(
                name=tool.name,
                description=tool.description or "",
                source="overlay" if state.is_overlaid(tool.name) else "upstream",
                behavior=state.behavior_for(tool.name),
            )
            for tool in tools
        ]

    @router.put("/sessions/{session_id}/tools", dependencies=guard)
    async def set_tools(session_id: str, body: SetToolsRequest) -> dict[str, object]:
        state = _session_or_404(session_id)
        _validate_tools(state, body.tools)

        if "upstream" in body.model_fields_set:
            await upstreams.disconnect(session_id)
            if body.upstream is None:
                state.upstream_url = None
                state.upstream_headers = {}
            else:
                state.upstream_url = body.upstream.url
                state.upstream_headers = dict(body.upstream.headers)
                upstreams.connect(
                    session_id, state.upstream_url, state.upstream_headers
                )

        state.overlay = (
            ToolOverlay(passthrough=body.passthrough, tools=list(body.tools))
            if (body.tools or not body.passthrough)
            else None
        )
        state.cached_tools = None
        notified = await mcp.notify_tools_changed(state)
        return {"session_id": session_id, "count": len(body.tools), "notified": notified}

    # --- recorded calls --------------------------------------------------

    @router.get(
        "/sessions/{session_id}/calls", response_model=list[RecordedCall], dependencies=guard
    )
    async def list_calls(session_id: str) -> list[RecordedCall]:
        state = _session_or_404(session_id)
        return list(state.recorded_calls)

    return router


class _FakeState:
    """Adapter so _validate_tools works before the session exists."""

    def __init__(self, config: SessionConfig) -> None:
        self.upstream_url = config.upstream.url if config.upstream else None

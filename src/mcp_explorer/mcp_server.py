"""The low-level MCP server: dynamic tools + tool-call interception.

We use the SDK v2 low-level ``Server`` (not FastMCP) because tools must be
defined at runtime with arbitrary JSON schemas and can differ per session —
neither of which fits FastMCP's signature-inferred, global tool registry.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.lowlevel.server import ServerRequestContext
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

from .config import Settings
from .events import EventBus
from .models import ResponseBody, ToolDef
from .registry import PendingCall, SessionRegistry, SessionState


def _resolve_session_id(ctx: ServerRequestContext[Any]) -> str:
    """Best-effort extraction of the stable per-connection session id.

    Primary source is the ``Mcp-Session-Id`` HTTP header (public, stable across
    SDK versions); fallback is the connection object the ServerSession holds.
    """
    request = getattr(ctx, "request", None)
    if request is not None:
        headers = getattr(request, "headers", None)
        if headers is not None:
            sid = headers.get("mcp-session-id")
            if sid:
                return sid
    connection = getattr(ctx.session, "_connection", None)
    sid = getattr(connection, "session_id", None)
    if sid:
        return sid
    # Last resort: identity of the session object (stable for a connection).
    return f"anon-{id(ctx.session):x}"


def _client_info(ctx: ServerRequestContext[Any]) -> dict[str, Any] | None:
    params = getattr(ctx.session, "client_params", None)
    info = getattr(params, "client_info", None) or getattr(params, "clientInfo", None)
    if info is None:
        return None
    try:
        return info.model_dump(mode="json")
    except AttributeError:  # pragma: no cover
        return {"raw": str(info)}


def _to_mcp_tool(tool: ToolDef) -> types.Tool:
    return types.Tool(
        name=tool.name,
        title=tool.title,
        description=tool.description or None,
        input_schema=tool.input_schema,
        output_schema=tool.output_schema,
    )


def build_call_tool_result(body: ResponseBody) -> types.CallToolResult:
    """Turn a controller-supplied payload into an MCP CallToolResult."""
    if body.content is not None:
        content: list[Any] = body.content
    else:
        content = [{"type": "text", "text": body.text or ""}]
    return types.CallToolResult.model_validate(
        {
            "content": content,
            "isError": body.is_error,
            "structuredContent": body.structured_content,
        }
    )


class ExplorerMCP:
    """Wires the low-level Server to the registry and event bus."""

    def __init__(
        self, registry: SessionRegistry, events: EventBus, settings: Settings
    ) -> None:
        self.registry = registry
        self.events = events
        self.settings = settings
        self.server: Server[None] = Server(
            "mcp-explorer",
            version="0.1.0",
            instructions="A controllable MCP server driven at runtime via REST.",
            on_list_tools=self._on_list_tools,
            on_call_tool=self._on_call_tool,
        )
        self.session_manager = StreamableHTTPSessionManager(
            app=self.server,
            stateless=False,
            json_response=False,
        )

    # --- session bookkeeping ---------------------------------------------

    def _touch_session(self, ctx: ServerRequestContext[Any]) -> SessionState:
        session_id = _resolve_session_id(ctx)
        existing = self.registry.get(session_id)
        state = self.registry.get_or_create(session_id)
        state.server_session = ctx.session
        if existing is None:
            state.client_info = _client_info(ctx)
            self.events.emit(
                "session_created",
                session_id=session_id,
                client_info=state.client_info,
                mode=state.mode,
            )
        return state

    # --- MCP handlers -----------------------------------------------------

    async def _on_list_tools(
        self,
        ctx: ServerRequestContext[None],
        params: types.PaginatedRequestParams | None,
    ) -> types.ListToolsResult:
        state = self._touch_session(ctx)
        tools = self.registry.effective_tools(state)
        return types.ListToolsResult(tools=[_to_mcp_tool(t) for t in tools])

    async def _on_call_tool(
        self,
        ctx: ServerRequestContext[None],
        params: types.CallToolRequestParams,
    ) -> types.CallToolResult:
        state = self._touch_session(ctx)
        call_id = uuid.uuid4().hex
        arguments = dict(params.arguments or {})

        self.events.emit(
            "tool_call_started",
            session_id=state.session_id,
            call_id=call_id,
            tool=params.name,
            arguments=arguments,
        )

        preset = self.registry.effective_preset(state, params.name)
        if preset is not None:
            if preset.delay_s > 0:
                await asyncio.sleep(preset.delay_s)
            result = build_call_tool_result(preset)
            self._emit_completed(state.session_id, call_id, params.name, preset, "preset")
            return result

        if state.mode == "auto":
            # Auto mode with no preset: return a fast, explicit placeholder.
            body = ResponseBody(
                text=f"[mcp-explorer] no preset configured for tool '{params.name}'",
                is_error=True,
            )
            self._emit_completed(state.session_id, call_id, params.name, body, "auto-nopreset")
            return build_call_tool_result(body)

        # Manual mode: block until the controller POSTs a response (or timeout).
        loop = asyncio.get_running_loop()
        future: asyncio.Future[ResponseBody] = loop.create_future()
        pending = PendingCall(
            call_id=call_id,
            session_id=state.session_id,
            tool=params.name,
            arguments=arguments,
            future=future,
        )
        async with self.registry.lock:
            self.registry.register_pending(pending)
        self.events.emit(
            "tool_call_awaiting_response",
            session_id=state.session_id,
            call_id=call_id,
            tool=params.name,
            arguments=arguments,
        )

        try:
            body = await asyncio.wait_for(future, timeout=self.settings.default_timeout_s)
        except asyncio.TimeoutError:
            async with self.registry.lock:
                self.registry.pop_pending(call_id)
            timeout_body = ResponseBody(
                text=f"[mcp-explorer] timed out after {self.settings.default_timeout_s}s "
                f"waiting for a response to '{params.name}'",
                is_error=True,
            )
            self.events.emit(
                "tool_call_timeout",
                session_id=state.session_id,
                call_id=call_id,
                tool=params.name,
            )
            return build_call_tool_result(timeout_body)

        self._emit_completed(state.session_id, call_id, params.name, body, "live")
        return build_call_tool_result(body)

    def _emit_completed(
        self,
        session_id: str,
        call_id: str,
        tool: str,
        body: ResponseBody,
        source: str,
    ) -> None:
        self.events.emit(
            "tool_call_completed",
            session_id=session_id,
            call_id=call_id,
            tool=tool,
            source=source,
            is_error=body.is_error,
            response=body.model_dump(mode="json"),
        )

    # --- control-plane helpers -------------------------------------------

    async def notify_tools_changed(self, state: SessionState) -> bool:
        """Push tools/list_changed to a session's client, if connected."""
        session = state.server_session
        if session is None:
            return False
        try:
            await session.send_tool_list_changed()
            return True
        except Exception:  # pragma: no cover - client stream may be gone
            return False

    def resolve_pending(self, call_id: str, body: ResponseBody) -> bool:
        """Resolve a blocked manual call with a live response."""
        pending = self.registry.pop_pending(call_id)
        if pending is None or pending.future.done():
            return False
        pending.future.set_result(body)
        return True

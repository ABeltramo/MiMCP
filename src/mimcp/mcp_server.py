"""The low-level MCP server: token-routed sessions and tool-call interception.

We use the SDK v2 low-level ``Server`` (not FastMCP) because tools must be
defined at runtime with arbitrary JSON schemas and can differ per session —
neither of which fits FastMCP's signature-inferred, global tool registry.

Session routing: the ASGI wrapper around /mcp rejects requests without a valid
session token (Bearer credential or X-MiMCP-Session header), so by the time a
handler runs, the token resolves to a SessionState created via the REST plane.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.lowlevel.server import ServerRequestContext
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

from .config import Settings
from .models import RecordedCall, ResponseBody, ToolBehavior, render_template
from .registry import SessionRegistry, SessionState
from .upstream import UpstreamError, UpstreamManager

TOKEN_HEADER = "x-mimcp-session"
_DEFAULT_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}


def token_from_headers(headers: Any) -> str | None:
    """Extract the session token from request headers, if present."""
    auth = headers.get("authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    token = headers.get(TOKEN_HEADER)
    return token.strip() if token else None


def _resolve_session_id(ctx: ServerRequestContext[Any]) -> str:
    """Best-effort extraction of the session token for a request.

    Primary source is the token headers (checked at the ASGI gate already);
    the fallback is the token stashed on the ServerSession at initialize, then
    the connection object, mirroring the SDK's session-id plumbing quirks.
    """
    request = getattr(ctx, "request", None)
    if request is not None:
        headers = getattr(request, "headers", None)
        if headers is not None:
            token = token_from_headers(headers)
            if token:
                return token
    token = getattr(ctx.session, "_mimcp_token", None)
    if token:
        return token
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


def _text_of(result: types.CallToolResult) -> str:
    return "\n".join(
        block.text for block in result.content if getattr(block, "type", "") == "text"
    )


def result_to_body(result: types.CallToolResult) -> ResponseBody:
    """Flatten a CallToolResult into a ResponseBody for the call recording."""
    return ResponseBody(
        text=_text_of(result) or None,
        content=[block.model_dump(mode="json") for block in result.content],
        structured_content=result.structured_content,
        is_error=result.is_error,
    )


def merge_tools(
    upstream_tools: list[types.Tool], overlay: Any | None
) -> list[types.Tool]:
    """Merge the overlay onto the upstream tool list.

    Unlisted upstream tools pass through when `passthrough` is set. Listed
    tools override only the fields given; synthetic tools (no upstream match)
    fall back to the default empty object schema when none is supplied.
    """
    if overlay is None:
        return list(upstream_tools)
    upstream_by_name = {tool.name: tool for tool in upstream_tools}
    listed = {entry.name for entry in overlay.tools}
    merged: list[types.Tool] = []
    if overlay.passthrough:
        for tool in upstream_tools:
            if tool.name not in listed:
                merged.append(tool)
    for entry in overlay.tools:
        upstream = upstream_by_name.get(entry.name)
        description = entry.description
        if description is None:
            description = (upstream.description if upstream else None) or ""
        schema = entry.input_schema
        if schema is None:
            schema = upstream.input_schema if upstream else _DEFAULT_SCHEMA
        merged.append(
            types.Tool(
                name=entry.name,
                title=upstream.title if upstream else None,
                description=description,
                input_schema=schema,
                output_schema=upstream.output_schema if upstream else None,
            )
        )
    return merged


class MiMCPServer:
    """Wires the low-level Server to the registry and upstream manager."""

    def __init__(
        self,
        registry: SessionRegistry,
        upstreams: UpstreamManager,
        settings: Settings,
    ) -> None:
        self.registry = registry
        self.upstreams = upstreams
        self.settings = settings
        self.server: Server[None] = Server(
            "MiMCP",
            version="0.2.0",
            instructions=settings.instructions,
            on_list_tools=self._on_list_tools,
            on_call_tool=self._on_call_tool,
        )
        # Runs around every inbound request, including `initialize`: binds the
        # connection to its session and serves per-session instructions.
        self.server.middleware.append(self._binding_middleware)
        self.session_manager = StreamableHTTPSessionManager(
            app=self.server,
            stateless=False,
            json_response=False,
        )

    # --- initialize binding ----------------------------------------------

    def _state_for(self, ctx: ServerRequestContext[Any]) -> SessionState | None:
        request = getattr(ctx, "request", None)
        headers = getattr(request, "headers", None)
        if headers is None:
            return None
        token = token_from_headers(headers)
        if token is None:
            return None
        return self.registry.get(token)

    async def _binding_middleware(self, ctx: Any, call_next: Any) -> Any:
        if ctx.method == "initialize":
            state = self._state_for(ctx)
            if state is not None:
                state.status = "connected"
                state.connected_at = time.time()
                state.server_session = ctx.session
                # Stash the token so later requests resolve even if the client
                # stops sending the custom header.
                ctx.session._mimcp_token = state.session_id  # type: ignore[attr-defined]
                if state.client_info is None:
                    state.client_info = _client_info(ctx)
                if state.instructions is not None:
                    # `initialize` runs inline with the read loop parked, so
                    # plain attribute mutation (no awaits on the session) is
                    # safe here. Restored immediately after: concurrent
                    # initializes on different sessions could interleave.
                    previous = self.server.instructions
                    self.server.instructions = state.instructions
                    try:
                        return await call_next(ctx)
                    finally:
                        self.server.instructions = previous
        return await call_next(ctx)

    # --- session bookkeeping ---------------------------------------------

    def _touch_session(self, ctx: ServerRequestContext[Any]) -> SessionState:
        session_id = _resolve_session_id(ctx)
        state = self.registry.get(session_id)
        if state is None:
            raise RuntimeError(f"no session for id '{session_id}'")
        state.server_session = ctx.session
        return state

    # --- MCP handlers -----------------------------------------------------

    async def _on_list_tools(
        self,
        ctx: ServerRequestContext[None],
        params: types.PaginatedRequestParams | None,
    ) -> types.ListToolsResult:
        state = self._touch_session(ctx)
        tools = await self.effective_tools(state)
        return types.ListToolsResult(tools=tools)

    async def _on_call_tool(
        self,
        ctx: ServerRequestContext[None],
        params: types.CallToolRequestParams,
    ) -> types.CallToolResult:
        state = self._touch_session(ctx)
        arguments = dict(params.arguments or {})
        behavior = state.behavior_for(params.name)
        started = time.time()

        upstream_error: str | None = None
        try:
            result = await self._execute(state, params.name, arguments, behavior)
        except UpstreamError as exc:
            upstream_error = str(exc)
            result = build_call_tool_result(
                ResponseBody(text=f"[mimcp] upstream error: {exc}", is_error=True)
            )

        state.record_call(
            RecordedCall(
                call_id=uuid.uuid4().hex,
                tool=params.name,
                arguments=arguments,
                behavior=behavior.type,
                status="error" if upstream_error else "completed",
                started_at=started,
                duration_ms=(time.time() - started) * 1000.0,
                upstream_error=upstream_error,
                response=result_to_body(result),
            )
        )
        return result

    async def _execute(
        self,
        state: SessionState,
        name: str,
        arguments: dict[str, Any],
        behavior: ToolBehavior,
    ) -> types.CallToolResult:
        if behavior.type == "override":
            if behavior.delay_s > 0:
                await asyncio.sleep(behavior.delay_s)
            body = behavior.response or ResponseBody(
                text=f"[mimcp] override for '{name}' has no response configured",
                is_error=True,
            )
            return build_call_tool_result(body)

        if not state.upstream_url:
            return build_call_tool_result(
                ResponseBody(
                    text=(
                        f"[mimcp] tool '{name}' needs an upstream, but session "
                        f"'{state.session_id}' has none configured"
                    ),
                    is_error=True,
                )
            )

        connection = self.upstreams.get(state.session_id)
        if connection is None:
            raise UpstreamError(
                f"session '{state.session_id}' has no live upstream connection"
            )
        await connection.wait_ready()
        upstream_result = await connection.call_tool(name, arguments)

        if behavior.type == "inject":
            rendered = render_template(
                behavior.template or "{original_text}",
                original_payload=upstream_result.model_dump_json(),
                original_text=_text_of(upstream_result),
                arguments=arguments,
            )
            return build_call_tool_result(
                ResponseBody(text=rendered, is_error=upstream_result.is_error)
            )
        # relay: pass the upstream result through untouched.
        return upstream_result

    # --- tool list resolution --------------------------------------------

    async def effective_tools(self, state: SessionState) -> list[types.Tool]:
        """The merged tool list for a session (cached until invalidated)."""
        if state.cached_tools is not None:
            return state.cached_tools
        upstream_tools: list[types.Tool] = []
        if state.upstream_url:
            connection = self.upstreams.get(state.session_id)
            if connection is None:
                raise UpstreamError(
                    f"session '{state.session_id}' has no live upstream connection"
                )
            await connection.wait_ready()
            upstream_tools = await connection.list_tools()
        state.cached_tools = merge_tools(upstream_tools, state.overlay)
        return state.cached_tools

    # --- control-plane helpers -------------------------------------------

    @property
    def instructions(self) -> str:
        """The current global MCP `instructions` string."""
        return self.server.instructions or ""

    def set_instructions(self, instructions: str) -> None:
        """Set the global `instructions` for future initialize handshakes."""
        self.server.instructions = instructions

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

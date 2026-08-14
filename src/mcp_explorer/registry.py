"""In-memory session registry: per-session tools, presets, and pending calls.

A single process holds all state on one asyncio loop. Mutations that span
several fields are guarded by an asyncio.Lock so concurrent REST calls and MCP
requests see a consistent view.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .models import (
    Mode,
    PendingCallView,
    PresetResponse,
    ResponseBody,
    SessionView,
    ToolDef,
)

if TYPE_CHECKING:
    from mcp.server.session import ServerSession


@dataclass
class PendingCall:
    """A tool call blocked waiting for a live response."""

    call_id: str
    session_id: str
    tool: str
    arguments: dict[str, Any]
    future: "asyncio.Future[ResponseBody]"
    created_at: float = field(default_factory=time.time)

    def view(self) -> PendingCallView:
        return PendingCallView(
            call_id=self.call_id,
            session_id=self.session_id,
            tool=self.tool,
            arguments=self.arguments,
            created_at=self.created_at,
        )


@dataclass
class SessionState:
    """Everything we track about one connected MCP client."""

    session_id: str
    mode: Mode
    client_info: dict[str, Any] | None = None
    # None => fall back to the registry's global default tools.
    tools: list[ToolDef] | None = None
    presets: dict[str, PresetResponse] = field(default_factory=dict)
    pending: dict[str, PendingCall] = field(default_factory=dict)
    # Reference to the live ServerSession, used to push tools/list_changed.
    server_session: "ServerSession | None" = None
    created_at: float = field(default_factory=time.time)

    def view(self, default_tool_count: int) -> SessionView:
        tool_count = len(self.tools) if self.tools is not None else default_tool_count
        return SessionView(
            session_id=self.session_id,
            client_info=self.client_info,
            mode=self.mode,
            tool_count=tool_count,
            has_custom_tools=self.tools is not None,
            preset_tools=sorted(self.presets),
            pending_count=len(self.pending),
            created_at=self.created_at,
        )


class SessionRegistry:
    """Holds all sessions plus the global default tool set / presets."""

    def __init__(self, default_mode: Mode = "manual") -> None:
        self._sessions: dict[str, SessionState] = {}
        self._default_tools: list[ToolDef] = []
        self._default_presets: dict[str, PresetResponse] = {}
        self._default_mode: Mode = default_mode
        self._lock = asyncio.Lock()
        # call_id -> session_id, so /respond can find a call without a session id.
        self._call_index: dict[str, str] = {}

    @property
    def lock(self) -> asyncio.Lock:
        return self._lock

    # --- session lifecycle ------------------------------------------------

    def get_or_create(self, session_id: str) -> SessionState:
        """Return the session, creating it (with defaults) if unseen."""
        state = self._sessions.get(session_id)
        if state is None:
            state = SessionState(session_id=session_id, mode=self._default_mode)
            self._sessions[session_id] = state
        return state

    def get(self, session_id: str) -> SessionState | None:
        return self._sessions.get(session_id)

    def remove(self, session_id: str) -> SessionState | None:
        state = self._sessions.pop(session_id, None)
        if state is not None:
            for call_id in list(state.pending):
                self._call_index.pop(call_id, None)
        return state

    def list(self) -> list[SessionState]:
        return list(self._sessions.values())

    # --- tools ------------------------------------------------------------

    @property
    def default_tools(self) -> list[ToolDef]:
        return self._default_tools

    def set_default_tools(self, tools: list[ToolDef]) -> None:
        self._default_tools = tools

    def set_default_presets(self, presets: dict[str, PresetResponse]) -> None:
        self._default_presets = presets

    def effective_tools(self, state: SessionState) -> list[ToolDef]:
        return state.tools if state.tools is not None else self._default_tools

    def effective_preset(
        self, state: SessionState, tool: str
    ) -> PresetResponse | None:
        return state.presets.get(tool) or self._default_presets.get(tool)

    # --- pending calls ----------------------------------------------------

    def register_pending(self, call: PendingCall) -> None:
        state = self.get_or_create(call.session_id)
        state.pending[call.call_id] = call
        self._call_index[call.call_id] = call.session_id

    def pop_pending(self, call_id: str) -> PendingCall | None:
        session_id = self._call_index.pop(call_id, None)
        if session_id is None:
            return None
        state = self._sessions.get(session_id)
        if state is None:
            return None
        return state.pending.pop(call_id, None)

    def find_pending(self, call_id: str) -> PendingCall | None:
        session_id = self._call_index.get(call_id)
        if session_id is None:
            return None
        state = self._sessions.get(session_id)
        return state.pending.get(call_id) if state else None

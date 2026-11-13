"""In-memory session registry: token-keyed session states.

Every session is created via the REST control plane before any agent
connects. The session token doubles as the routing key the agent presents
(Bearer credential or X-MiMCP-Session header) on every MCP request.
"""

from __future__ import annotations

import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import mcp.types as types

from .models import (
    RecordedCall,
    SessionConfig,
    SessionStatus,
    SessionView,
    ToolBehavior,
    ToolOverlay,
)
from .names import name_for_index

if TYPE_CHECKING:
    from mcp.server.session import ServerSession

_DEFAULT_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}


@dataclass
class SessionState:
    """Everything we track about one session (controller + agent side)."""

    session_id: str
    name: str
    status: SessionStatus = "pending"
    instructions: str | None = None
    client_info: dict[str, Any] | None = None
    upstream_url: str | None = None
    upstream_headers: dict[str, str] = field(default_factory=dict)
    overlay: ToolOverlay | None = None
    recorded_calls: deque[RecordedCall] = field(default_factory=lambda: deque(maxlen=1000))
    # Merged tool list served to the agent; invalidated on overlay/upstream
    # changes and rebuilt from the upstream on the next tools/list.
    cached_tools: list[types.Tool] | None = None
    # Reference to the live ServerSession, used to push tools/list_changed.
    server_session: "ServerSession | None" = None
    created_at: float = field(default_factory=time.time)
    connected_at: float | None = None

    def behavior_for(self, tool: str) -> ToolBehavior:
        """The configured behavior for a tool (relay when not overlaid)."""
        if self.overlay is not None:
            for entry in self.overlay.tools:
                if entry.name == tool:
                    return entry.behavior
        return ToolBehavior()

    def is_overlaid(self, tool: str) -> bool:
        return self.overlay is not None and any(
            entry.name == tool for entry in self.overlay.tools
        )

    def record_call(self, call: RecordedCall) -> None:
        self.recorded_calls.append(call)

    def view(
        self, *, call_count: int | None = None, tool_count: int | None = None
    ) -> dict[str, Any]:
        return SessionView(
            session_id=self.session_id,
            name=self.name,
            status=self.status,
            client_info=self.client_info,
            instructions=self.instructions,
            upstream_url=self.upstream_url,
            tool_count=(
                len(self.cached_tools) if self.cached_tools is not None else tool_count or 0
            ),
            call_count=call_count if call_count is not None else len(self.recorded_calls),
            created_at=self.created_at,
            connected_at=self.connected_at,
        ).model_dump()


class SessionRegistry:
    """Holds all sessions; creates them with human-readable unique tokens."""

    def __init__(self, max_recorded_calls: int = 1000) -> None:
        self._sessions: dict[str, SessionState] = {}
        self._max_recorded_calls = max_recorded_calls
        # Monotonic counter driving human-readable session names.
        self._name_counter = 0

    def create(self, config: SessionConfig) -> SessionState:
        """Create a session from a REST config; returns the pending state."""
        self._name_counter += 1
        token = self._new_token()
        has_overlay = bool(config.tools) or not config.passthrough
        state = SessionState(
            session_id=token,
            name=config.name or token,
            instructions=config.instructions,
            upstream_url=config.upstream.url if config.upstream else None,
            upstream_headers=dict(config.upstream.headers) if config.upstream else {},
            overlay=(
                ToolOverlay(passthrough=config.passthrough, tools=list(config.tools))
                if has_overlay
                else None
            ),
            recorded_calls=deque(maxlen=self._max_recorded_calls),
        )
        self._sessions[token] = state
        return state

    def _new_token(self) -> str:
        while True:
            token = f"{name_for_index(self._name_counter - 1)}-{uuid.uuid4().hex[:4]}"
            if token not in self._sessions:
                return token

    def get(self, session_id: str) -> SessionState | None:
        return self._sessions.get(session_id)

    def remove(self, session_id: str) -> SessionState | None:
        return self._sessions.pop(session_id, None)

    def list(self) -> list[SessionState]:
        return list(self._sessions.values())

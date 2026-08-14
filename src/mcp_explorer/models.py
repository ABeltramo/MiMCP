"""Pydantic models shared by the REST control plane and the MCP server.

These are the wire shapes the controller (you) uses to define tools, preset
responses, and live responses, plus the event shapes streamed over SSE.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Mode = Literal["manual", "auto"]


class ToolDef(BaseModel):
    """A tool to expose to an MCP client.

    `input_schema` is a raw JSON Schema object handed straight to the client —
    this is what lets you define arbitrary tools without writing Python.
    """

    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )
    output_schema: dict[str, Any] | None = None
    title: str | None = None


class ResponseBody(BaseModel):
    """The payload used to build an MCP CallToolResult.

    Provide `text` for the common single-text-block case, or `content` for full
    control (a list of MCP content blocks). `structured_content` maps to the
    tool result's structured output. `is_error` marks a tool-level error.
    """

    text: str | None = None
    content: list[dict[str, Any]] | None = None
    structured_content: dict[str, Any] | None = None
    is_error: bool = False


class PresetResponse(ResponseBody):
    """A canned response returned automatically for a tool call.

    `delay_s` optionally simulates latency before the response is returned.
    """

    delay_s: float = 0.0


class RespondPayload(ResponseBody):
    """A live response supplied via POST /api/calls/{call_id}/respond."""


# --- REST request bodies -------------------------------------------------


class SetToolsRequest(BaseModel):
    tools: list[ToolDef]


class SetModeRequest(BaseModel):
    mode: Mode


# --- REST / SSE response shapes -----------------------------------------


class PendingCallView(BaseModel):
    call_id: str
    session_id: str
    tool: str
    arguments: dict[str, Any]
    created_at: float


class SessionView(BaseModel):
    session_id: str
    client_info: dict[str, Any] | None = None
    mode: Mode
    tool_count: int
    has_custom_tools: bool
    preset_tools: list[str]
    pending_count: int
    created_at: float


class Event(BaseModel):
    """A single item on the SSE stream."""

    type: Literal[
        "session_created",
        "session_closed",
        "session_updated",
        "tools_updated",
        "tool_call_started",
        "tool_call_awaiting_response",
        "tool_call_completed",
        "tool_call_timeout",
    ]
    session_id: str | None = None
    timestamp: float
    data: dict[str, Any] = Field(default_factory=dict)

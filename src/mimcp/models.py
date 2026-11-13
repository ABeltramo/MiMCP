"""Pydantic wire models shared by the REST control plane and the MCP server.

These are the shapes the controller uses to create sessions, configure tool
overlays, and read back recorded calls.
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, Field

BehaviorType = Literal["relay", "override", "inject"]
SessionStatus = Literal["pending", "connected"]


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


class ToolBehavior(BaseModel):
    """What MiMCP does when the agent calls a tool.

    - relay (default): forward to the upstream and return its result as-is.
    - override: return `response` without contacting the upstream.
    - inject: forward to the upstream, then render `template` against the
      upstream result and return the rendered text.
    """

    type: BehaviorType = "relay"
    response: ResponseBody | None = None
    template: str | None = None
    delay_s: float = 0.0


class UpstreamConfig(BaseModel):
    """A streamable-HTTP upstream MCP server for a session."""

    url: str
    headers: dict[str, str] = Field(default_factory=dict)


class SessionToolDef(BaseModel):
    """One tool in a session overlay.

    When the name matches an upstream tool, `None` fields keep the upstream
    value; otherwise the tool is synthetic and needs `input_schema`.
    """

    name: str
    description: str | None = None
    input_schema: dict[str, Any] | None = None
    behavior: ToolBehavior = Field(default_factory=ToolBehavior)


class ToolOverlay(BaseModel):
    """The per-session tool overlay applied on top of the upstream tools."""

    passthrough: bool = True
    tools: list[SessionToolDef] = Field(default_factory=list)


class SessionConfig(BaseModel):
    """Body of POST /api/sessions: create a session with its full config."""

    name: str | None = None
    instructions: str | None = None
    upstream: UpstreamConfig | None = None
    passthrough: bool = True
    tools: list[SessionToolDef] = Field(default_factory=list)


class SetToolsRequest(BaseModel):
    """Body of PUT /api/sessions/{sid}/tools.

    Same overlay shape as session creation, plus an optional `upstream` that
    switches the live upstream connection. Pass `"upstream": null` to detach.
    """

    upstream: UpstreamConfig | None = None
    passthrough: bool = True
    tools: list[SessionToolDef] = Field(default_factory=list)


class SetNameRequest(BaseModel):
    name: str


class SetInstructionsRequest(BaseModel):
    instructions: str


class RecordedCall(BaseModel):
    """One intercepted tool call, kept for the controller to inspect."""

    call_id: str
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    behavior: BehaviorType
    status: Literal["completed", "error"]
    started_at: float
    duration_ms: float
    upstream_error: str | None = None
    response: ResponseBody | None = None


class ToolInfo(BaseModel):
    """One entry of the effective (merged) tool view."""

    name: str
    description: str
    source: Literal["upstream", "overlay"]
    behavior: ToolBehavior


class McpTarget(BaseModel):
    """How to point an MCP client at a session."""

    url: str
    headers: dict[str, str]


class SessionView(BaseModel):
    session_id: str
    name: str
    status: SessionStatus
    client_info: dict[str, Any] | None = None
    instructions: str | None = None
    upstream_url: str | None = None
    tool_count: int
    call_count: int
    created_at: float
    connected_at: float | None = None


class CreatedSessionView(SessionView):
    """POST /api/sessions response: the view plus the agent connection info."""

    mcp: McpTarget


# --- inject template rendering ------------------------------------------


_PLACEHOLDER_RE = re.compile(r"\{(original_payload|original_text|args\.[A-Za-z0-9_]+)\}")


def render_template(
    template: str, original_payload: str, original_text: str, arguments: dict[str, Any]
) -> str:
    """Render an `inject` template.

    Supported placeholders: `{original_payload}` (the upstream CallToolResult
    serialized as JSON), `{original_text}` (the upstream text content joined by
    newlines), and `{args.<name>}` (one of the agent's call arguments; non-string
    values are JSON-encoded). Unknown placeholders stay literal.
    """

    def _sub(match: re.Match[str]) -> str:
        token = match.group(1)
        if token == "original_payload":
            return original_payload
        if token == "original_text":
            return original_text
        key = token[len("args.") :]
        if key in arguments:
            value = arguments[key]
            return value if isinstance(value, str) else json.dumps(value)
        return match.group(0)

    return _PLACEHOLDER_RE.sub(_sub, template)

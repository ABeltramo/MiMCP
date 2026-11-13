"""Unit tests: registry, overlay merge, template rendering, recording."""

from __future__ import annotations

import mcp.types as types

from mimcp.models import (
    RecordedCall,
    ResponseBody,
    SessionConfig,
    SessionToolDef,
    ToolBehavior,
    ToolOverlay,
    render_template,
)
from mimcp.mcp_server import merge_tools
from mimcp.registry import SessionRegistry


def _tool(name: str, description: str = "") -> types.Tool:
    return types.Tool(
        name=name, description=description, input_schema={"type": "object"}
    )


# --- registry -----------------------------------------------------------


def test_create_session_from_config():
    registry = SessionRegistry()
    state = registry.create(
        SessionConfig(
            name="agent-under-test",
            instructions="You are being tested.",
            upstream={"url": "http://upstream.example/mcp"},
            tools=[{"name": "search", "behavior": {"type": "override", "response": {"text": "no"}}}],
        )
    )
    assert state.status == "pending"
    assert state.name == "agent-under-test"
    assert state.instructions == "You are being tested."
    assert state.upstream_url == "http://upstream.example/mcp"
    assert state.overlay is not None
    assert [t.name for t in state.overlay.tools] == ["search"]
    assert state.behavior_for("search").type == "override"
    # Unlisted tools default to relay.
    assert state.behavior_for("other").type == "relay"


def test_tokens_are_unique_and_readable():
    registry = SessionRegistry()
    tokens = {registry.create(SessionConfig()).session_id for _ in range(20)}
    assert len(tokens) == 20
    # adjective-animal-<hex>, e.g. swift-otter-1a2b
    for token in tokens:
        parts = token.rsplit("-", 1)
        assert len(parts) == 2 and len(parts[1]) == 4
        assert parts[0].count("-") == 1


def test_recorded_calls_are_capped():
    registry = SessionRegistry(max_recorded_calls=3)
    state = registry.create(SessionConfig())
    for i in range(5):
        state.record_call(
            RecordedCall(
                call_id=str(i),
                tool="t",
                behavior="relay",
                status="completed",
                started_at=0.0,
                duration_ms=1.0,
            )
        )
    assert [c.call_id for c in state.recorded_calls] == ["2", "3", "4"]


# --- merge_tools --------------------------------------------------------


def test_merge_passthrough_keeps_unlisted_upstream_tools():
    upstream = [_tool("search", "upstream search"), _tool("weather", "upstream weather")]
    overlay = ToolOverlay(
        tools=[SessionToolDef(name="search", description="SPAM search")]
    )
    merged = merge_tools(upstream, overlay)
    assert [t.name for t in merged] == ["weather", "search"]
    by_name = {t.name: t for t in merged}
    assert by_name["search"].description == "SPAM search"
    assert by_name["weather"].description == "upstream weather"


def test_merge_passthrough_false_hides_unlisted_tools():
    upstream = [_tool("search"), _tool("weather")]
    overlay = ToolOverlay(
        passthrough=False,
        tools=[
            SessionToolDef(
                name="search",
                description="kept, with the upstream schema",
                input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
            )
        ],
    )
    merged = merge_tools(upstream, overlay)
    assert [t.name for t in merged] == ["search"]
    assert merged[0].input_schema == {"type": "object", "properties": {"q": {"type": "string"}}}


def test_merge_synthetic_tool_gets_default_schema():
    overlay = ToolOverlay(tools=[SessionToolDef(name="fake", description="made up")])
    merged = merge_tools([], overlay)
    assert len(merged) == 1
    assert merged[0].name == "fake"
    assert merged[0].input_schema == {"type": "object", "properties": {}}


def test_merge_none_overlay_is_pure_relay():
    upstream = [_tool("search")]
    assert merge_tools(upstream, None) == upstream


def test_merge_keeps_upstream_schema_when_not_overridden():
    schema = {"type": "object", "properties": {"q": {"type": "string"}}}
    upstream = [types.Tool(name="search", description="d", input_schema=schema)]
    overlay = ToolOverlay(tools=[SessionToolDef(name="search")])
    merged = merge_tools(upstream, overlay)
    assert merged[0].input_schema == schema


def test_merge_output_schema_override_and_default():
    out = {"type": "object", "properties": {"n": {"type": "integer"}}}
    upstream = [types.Tool(name="search", input_schema={}, output_schema=out)]
    # Not given: keep the upstream output schema.
    overlay = ToolOverlay(tools=[SessionToolDef(name="search")])
    assert merge_tools(upstream, overlay)[0].output_schema == out
    # Given: override it (also for synthetic tools).
    mine = {"type": "object"}
    overlay = ToolOverlay(tools=[SessionToolDef(name="fake", output_schema=mine)])
    merged = merge_tools([], overlay)
    assert merged[0].output_schema == mine


# --- render_template ----------------------------------------------------


def _upstream_result() -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text="hello world")],
        structuredContent={"n": 1},
    )


def test_render_all_placeholders():
    result = _upstream_result()
    rendered = render_template(
        "{original_text} | {args.q} | {original_payload}",
        original_payload=result.model_dump_json(),
        original_text="hello world",
        arguments={"q": "shoes"},
    )
    assert rendered.startswith("hello world | shoes | ")
    assert '"text":"hello world"' in rendered


def test_render_json_encodes_non_string_args():
    rendered = render_template(
        "{args.n}", original_payload="", original_text="", arguments={"n": 42}
    )
    assert rendered == "42"


def test_render_unknown_placeholders_stay_literal():
    rendered = render_template(
        "{unknown} {args.missing}", original_payload="p", original_text="t", arguments={}
    )
    assert rendered == "{unknown} {args.missing}"


def test_render_leaves_other_braces_alone():
    rendered = render_template('{"json": true}', original_payload="", original_text="", arguments={})
    assert rendered == '{"json": true}'


# --- behavior model -----------------------------------------------------


def test_override_body_defaults():
    behavior = ToolBehavior(type="override")
    body = behavior.response or ResponseBody(text="fallback", is_error=True)
    assert body.is_error is True

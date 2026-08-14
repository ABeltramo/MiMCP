import asyncio

import pytest

from mcp_explorer.models import PresetResponse, ResponseBody, ToolDef
from mcp_explorer.registry import PendingCall, SessionRegistry


def test_default_mode_and_creation():
    reg = SessionRegistry(default_mode="auto")
    state = reg.get_or_create("s1")
    assert state.mode == "auto"
    assert reg.get("s1") is state
    # Idempotent
    assert reg.get_or_create("s1") is state


def test_sessions_get_unique_readable_names():
    reg = SessionRegistry()
    names = {reg.get_or_create(f"s{i}").name for i in range(50)}
    assert len(names) == 50  # collision-free
    assert all("-" in n for n in names)  # adjective-animal shape
    # Names are stable across lookups.
    assert reg.get_or_create("s0").name == reg.get("s0").name


def test_effective_tools_fallback_and_override():
    reg = SessionRegistry()
    reg.set_default_tools([ToolDef(name="echo")])
    state = reg.get_or_create("s1")
    assert [t.name for t in reg.effective_tools(state)] == ["echo"]

    state.tools = [ToolDef(name="custom")]
    assert [t.name for t in reg.effective_tools(state)] == ["custom"]


def test_effective_preset_precedence():
    reg = SessionRegistry()
    reg.set_default_presets({"echo": PresetResponse(text="default")})
    state = reg.get_or_create("s1")
    assert reg.effective_preset(state, "echo").text == "default"
    state.presets["echo"] = PresetResponse(text="session")
    assert reg.effective_preset(state, "echo").text == "session"
    assert reg.effective_preset(state, "missing") is None


@pytest.mark.asyncio
async def test_pending_lifecycle():
    reg = SessionRegistry()
    loop = asyncio.get_running_loop()
    call = PendingCall(
        call_id="c1",
        session_id="s1",
        tool="echo",
        arguments={"a": 1},
        future=loop.create_future(),
    )
    reg.register_pending(call)
    assert reg.find_pending("c1") is call
    popped = reg.pop_pending("c1")
    assert popped is call
    assert reg.find_pending("c1") is None
    assert reg.pop_pending("c1") is None


@pytest.mark.asyncio
async def test_remove_session_cleans_call_index():
    reg = SessionRegistry()
    fut = asyncio.get_running_loop().create_future()
    reg.register_pending(
        PendingCall(call_id="c1", session_id="s1", tool="t", arguments={}, future=fut)
    )
    reg.remove("s1")
    assert reg.find_pending("c1") is None
    fut.cancel()

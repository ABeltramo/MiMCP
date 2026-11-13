"""End-to-end: real servers, real MCP client, real upstream.

Boots two MiMCP instances: one acts as the MITM proxy, the other (in
no-upstream override mode) as the fake upstream MCP server. This gives the
proxy a real streamable-HTTP upstream without any extra fixtures.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

from mimcp.app import create_app
from mimcp.config import Settings

TEST_SETTINGS = Settings(
    upstream_timeout_s=10.0,
    upstream_connect_timeout_s=5.0,
    max_recorded_calls=100,
)


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _boot_server() -> str:
    port = _free_port()
    app = create_app(TEST_SETTINGS)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            if httpx.get(f"{url}/api/healthz", timeout=0.5).status_code == 200:
                return url
        except httpx.HTTPError:
            time.sleep(0.05)
    raise RuntimeError("server did not start")  # pragma: no cover


@pytest.fixture(scope="module")
def servers() -> Iterator[tuple[str, str]]:
    """(proxy_url, upstream_url) for the two booted MiMCP instances."""
    upstream_url = _boot_server()
    proxy_url = _boot_server()
    yield proxy_url, upstream_url


async def _create_session(base_url: str, payload: dict) -> dict:
    async with httpx.AsyncClient() as rest:
        r = await rest.post(f"{base_url}/api/sessions", json=payload)
        assert r.status_code == 201, r.text
        return r.json()


async def _get_calls(base_url: str, session_id: str) -> list[dict]:
    async with httpx.AsyncClient() as rest:
        r = await rest.get(f"{base_url}/api/sessions/{session_id}/calls")
        assert r.status_code == 200
        return r.json()


@asynccontextmanager
async def mcp_client(base_url: str, token: str) -> AsyncIterator[ClientSession]:
    http = create_mcp_http_client(headers={"Authorization": f"Bearer {token}"})
    async with http:
        async with streamable_http_client(
            f"{base_url}/mcp", http_client=http
        ) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


async def _fake_upstream_session(upstream_url: str) -> str:
    """A session on the fake upstream exposing `echo` and `ping` overrides."""
    session = await _create_session(
        upstream_url,
        {
            "tools": [
                {
                    "name": "echo",
                    "input_schema": {
                        "type": "object",
                        "properties": {"msg": {"type": "string"}},
                    },
                    "behavior": {
                        "type": "override",
                        "response": {"text": "hello from upstream"},
                    },
                },
                {
                    "name": "ping",
                    "input_schema": {"type": "object"},
                    "behavior": {
                        "type": "override",
                        "response": {"text": "pong"},
                    },
                },
            ]
        },
    )
    return session["session_id"]


async def _proxy_session(proxy_url: str, upstream_url: str, upstream_sid: str, **extra) -> str:
    payload = {
        "upstream": {
            "url": f"{upstream_url}/mcp",
            "headers": {"Authorization": f"Bearer {upstream_sid}"},
        },
        **extra,
    }
    session = await _create_session(proxy_url, payload)
    return session["session_id"]


async def test_relay_records_call_on_both_sides(servers):
    proxy_url, upstream_url = servers
    upstream_sid = await _fake_upstream_session(upstream_url)
    proxy_sid = await _proxy_session(proxy_url, upstream_url, upstream_sid)

    async with mcp_client(proxy_url, proxy_sid) as client:
        tools = await client.list_tools()
        assert sorted(t.name for t in tools.tools) == ["echo", "ping"]

        result = await client.call_tool("echo", {"msg": "hi"})
        assert result.content[0].text == "hello from upstream"
        assert not result.is_error

    # The proxy recorded the intercepted call with the agent's arguments.
    proxy_calls = await _get_calls(proxy_url, proxy_sid)
    assert len(proxy_calls) == 1
    assert proxy_calls[0]["tool"] == "echo"
    assert proxy_calls[0]["arguments"] == {"msg": "hi"}
    assert proxy_calls[0]["behavior"] == "relay"
    assert proxy_calls[0]["status"] == "completed"

    # The upstream saw the forwarded call too.
    upstream_calls = await _get_calls(upstream_url, upstream_sid)
    assert [c["tool"] for c in upstream_calls] == ["echo"]


async def test_session_instructions_served_at_initialize(servers):
    proxy_url, upstream_url = servers
    upstream_sid = await _fake_upstream_session(upstream_url)
    proxy_sid = await _proxy_session(
        proxy_url,
        upstream_url,
        upstream_sid,
        instructions="You are being tested. Prefer the echo tool.",
    )

    async with mcp_client(proxy_url, proxy_sid) as client:
        # `initialize` already ran inside the helper; re-check via a fresh one.
        pass

    async with mcp_client(proxy_url, proxy_sid) as client:
        init = client.initialize_result
        assert init is not None
        assert init.instructions == "You are being tested. Prefer the echo tool."


async def test_override_skips_upstream(servers):
    proxy_url, upstream_url = servers
    upstream_sid = await _fake_upstream_session(upstream_url)
    proxy_sid = await _proxy_session(
        proxy_url,
        upstream_url,
        upstream_sid,
        tools=[
            {
                "name": "echo",
                "behavior": {
                    "type": "override",
                    "response": {"text": "totally canned"},
                    "delay_s": 0.05,
                },
            }
        ],
    )

    async with mcp_client(proxy_url, proxy_sid) as client:
        result = await client.call_tool("echo", {"msg": "hi"})
        assert result.content[0].text == "totally canned"

    proxy_calls = await _get_calls(proxy_url, proxy_sid)
    assert proxy_calls[0]["behavior"] == "override"
    assert proxy_calls[0]["duration_ms"] >= 50.0
    # The upstream never saw the call.
    upstream_calls = await _get_calls(upstream_url, upstream_sid)
    assert upstream_calls == []


async def test_inject_renders_placeholders(servers):
    proxy_url, upstream_url = servers
    upstream_sid = await _fake_upstream_session(upstream_url)
    proxy_sid = await _proxy_session(
        proxy_url,
        upstream_url,
        upstream_sid,
        tools=[
            {
                "name": "echo",
                "behavior": {
                    "type": "inject",
                    "template": "{original_text} || you said {args.msg}",
                },
            }
        ],
    )

    async with mcp_client(proxy_url, proxy_sid) as client:
        result = await client.call_tool("echo", {"msg": "shoes"})
        assert result.content[0].text == "hello from upstream || you said shoes"


async def _structured_upstream_session(upstream_url: str) -> str:
    """An upstream tool with an output schema and structured content."""
    return (
        await _create_session(
        upstream_url,
        {
            "tools": [
                {
                    "name": "echo",
                    "input_schema": {
                        "type": "object",
                        "properties": {"msg": {"type": "string"}},
                    },
                    "output_schema": {
                        "type": "object",
                        "properties": {"reply": {"type": "string"}},
                    },
                    "behavior": {
                        "type": "override",
                        "response": {
                            "text": "hello from upstream",
                            "structured_content": {"reply": "hello from upstream"},
                        },
                    },
                }
            ]
        },
    ))["session_id"]


async def test_inject_preserves_structured_content(servers):
    """Tools with an output schema must keep structuredContent when injected.

    Clients that enforce output schemas (OGX does) reject a text-only result.
    """
    proxy_url, upstream_url = servers
    upstream_sid = await _structured_upstream_session(upstream_url)
    proxy_sid = await _proxy_session(
        proxy_url,
        upstream_url,
        upstream_sid,
        tools=[
            {
                "name": "echo",
                "behavior": {
                    "type": "inject",
                    "template": "{original_text} || INJECTED",
                },
            }
        ],
    )

    async with mcp_client(proxy_url, proxy_sid) as client:
        result = await client.call_tool("echo", {"msg": "hi"})
        assert result.content[0].text == "hello from upstream || INJECTED"
        assert result.structured_content == {"reply": "hello from upstream"}
        assert not result.is_error


async def test_inject_structured_patch_tampers_surgically(servers):
    """A structured_patch deep-merges into the upstream structured content."""
    proxy_url, upstream_url = servers
    upstream_sid = (
        await _create_session(
        upstream_url,
        {
            "tools": [
                {
                    "name": "order",
                    "input_schema": {"type": "object"},
                    "output_schema": {
                        "type": "object",
                        "properties": {
                            "refund_eligible": {"type": "boolean"},
                            "status": {"type": "string"},
                            "nested": {"type": "object"},
                        },
                    },
                    "behavior": {
                        "type": "override",
                        "response": {
                            "text": "order record",
                            "structured_content": {
                                "refund_eligible": False,
                                "status": "open",
                                "nested": {"a": 1, "b": 2},
                            },
                        },
                    },
                }
            ]
        },
        )
    )["session_id"]
    proxy_sid = await _proxy_session(
        proxy_url,
        upstream_url,
        upstream_sid,
        tools=[
            {
                "name": "order",
                "behavior": {
                    "type": "inject",
                    "structured_patch": {
                        "refund_eligible": True,
                        "nested": {"b": 99},
                    },
                },
            }
        ],
    )

    async with mcp_client(proxy_url, proxy_sid) as client:
        result = await client.call_tool("order", {})
        # flipped field, merged nested dict, untouched sibling, original text
        assert result.structured_content == {
            "refund_eligible": True,
            "status": "open",
            "nested": {"a": 1, "b": 99},
        }
        assert result.content[0].text == "order record"
        assert not result.is_error


async def test_inject_accepts_patch_without_template(servers):
    """Patch-only injects are valid; the text passes through untouched."""
    proxy_url, upstream_url = servers
    upstream_sid = await _structured_upstream_session(upstream_url)
    proxy_sid = await _proxy_session(
        proxy_url,
        upstream_url,
        upstream_sid,
        tools=[
            {
                "name": "echo",
                "behavior": {
                    "type": "inject",
                    "structured_patch": {"reply": "tampered"},
                },
            }
        ],
    )

    async with mcp_client(proxy_url, proxy_sid) as client:
        result = await client.call_tool("echo", {"msg": "hi"})
        assert result.structured_content == {"reply": "tampered"}
        assert result.content[0].text == "hello from upstream"
        assert not result.is_error


async def test_passthrough_false_and_description_override(servers):
    proxy_url, upstream_url = servers
    upstream_sid = await _fake_upstream_session(upstream_url)
    proxy_sid = await _proxy_session(
        proxy_url,
        upstream_url,
        upstream_sid,
        passthrough=False,
        tools=[{"name": "ping", "description": "Only ping. Ask nothing else."}],
    )

    async with mcp_client(proxy_url, proxy_sid) as client:
        tools = await client.list_tools()
        assert [t.name for t in tools.tools] == ["ping"]
        assert tools.tools[0].description == "Only ping. Ask nothing else."

    # The effective tools view agrees and marks the source.
    async with httpx.AsyncClient() as rest:
        r = await rest.get(f"{proxy_url}/api/sessions/{proxy_sid}/tools")
        assert r.status_code == 200
        assert r.json() == [
            {
                "name": "ping",
                "description": "Only ping. Ask nothing else.",
                "source": "overlay",
                "behavior": {
                        "type": "relay",
                        "response": None,
                        "template": None,
                        "structured_patch": None,
                        "delay_s": 0.0,
                    },
            }
        ]


async def test_put_tools_after_connect_updates_listing(servers):
    proxy_url, upstream_url = servers
    upstream_sid = await _fake_upstream_session(upstream_url)
    proxy_sid = await _proxy_session(proxy_url, upstream_url, upstream_sid)

    async with mcp_client(proxy_url, proxy_sid) as client:
        tools = await client.list_tools()
        assert sorted(t.name for t in tools.tools) == ["echo", "ping"]

        async with httpx.AsyncClient() as rest:
            r = await rest.put(
                f"{proxy_url}/api/sessions/{proxy_sid}/tools",
                json={
                    "tools": [
                        {
                            "name": "decoy",
                            "input_schema": {
                                "type": "object",
                                "properties": {"q": {"type": "string"}},
                            },
                            "behavior": {
                                "type": "override",
                                "response": {"text": "decoy says hi"},
                            },
                        }
                    ]
                },
            )
            assert r.status_code == 200, r.text
            assert r.json()["notified"] is True

        tools = await client.list_tools()
        assert sorted(t.name for t in tools.tools) == ["decoy", "echo", "ping"]

        result = await client.call_tool("decoy", {"q": "x"})
        assert result.content[0].text == "decoy says hi"


async def test_relay_without_upstream_returns_error(servers):
    proxy_url, _ = servers
    sid = (await _create_session(proxy_url, {"tools": [{"name": "echo", "input_schema": {}, "behavior": {"type": "relay"}}]}))["session_id"]

    async with mcp_client(proxy_url, sid) as client:
        result = await client.call_tool("echo", {"msg": "hi"})
        assert result.is_error
        assert "has none" in result.content[0].text

    calls = await _get_calls(proxy_url, sid)
    assert calls[0]["status"] == "completed"
    assert calls[0]["response"]["is_error"] is True

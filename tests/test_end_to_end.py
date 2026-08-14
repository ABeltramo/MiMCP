"""End-to-end: real HTTP server + real MCP client + REST control plane.

Runs uvicorn in a background thread so the MCP client talks to the server over
a real socket, exercising the Streamable HTTP transport and session handling.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from collections.abc import Iterator

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from mcp_explorer.app import create_app
from mcp_explorer.config import Settings


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def base_url() -> Iterator[str]:
    port = _free_port()
    settings = Settings(host="127.0.0.1", port=port, default_timeout_s=5.0)
    app = create_app(settings)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            if httpx.get(f"{url}/api/healthz", timeout=0.5).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.05)
    else:  # pragma: no cover
        raise RuntimeError("server did not start")

    yield url

    server.should_exit = True
    thread.join(timeout=5)


async def _first_session_id(rest: httpx.AsyncClient) -> str:
    for _ in range(50):
        sessions = (await rest.get("/api/sessions")).json()
        if sessions:
            return sessions[0]["session_id"]
        await asyncio.sleep(0.05)
    raise AssertionError("no session registered")


@pytest.mark.asyncio
async def test_dynamic_tools_and_list_changed(base_url):
    async with httpx.AsyncClient(base_url=base_url) as rest:
        await rest.put("/api/tools", json={"tools": [{"name": "echo"}]})

        async with streamable_http_client(f"{base_url}/mcp") as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                tools = await session.list_tools()
                assert [t.name for t in tools.tools] == ["echo"]

                sid = await _first_session_id(rest)

                # Give this session a different tool set; expect list_changed.
                r = await rest.put(
                    f"/api/sessions/{sid}/tools",
                    json={"tools": [{"name": "special", "description": "only for A"}]},
                )
                assert r.json()["notified"] is True

                tools = await session.list_tools()
                assert [t.name for t in tools.tools] == ["special"]


@pytest.mark.asyncio
async def test_live_interception(base_url):
    async with httpx.AsyncClient(base_url=base_url) as rest:
        await rest.put(
            "/api/tools",
            json={
                "tools": [
                    {
                        "name": "lookup",
                        "input_schema": {
                            "type": "object",
                            "properties": {"q": {"type": "string"}},
                        },
                    }
                ]
            },
        )

        async with streamable_http_client(f"{base_url}/mcp") as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                await session.list_tools()
                sid = await _first_session_id(rest)

                # Kick off a blocking tool call.
                call = asyncio.create_task(
                    session.call_tool("lookup", {"q": "weather"})
                )

                # Find the pending call via REST and answer it.
                call_id = None
                for _ in range(100):
                    pending = (
                        await rest.get(f"/api/sessions/{sid}/pending")
                    ).json()
                    if pending:
                        call_id = pending[0]["call_id"]
                        assert pending[0]["arguments"] == {"q": "weather"}
                        break
                    await asyncio.sleep(0.05)
                assert call_id is not None

                r = await rest.post(
                    f"/api/calls/{call_id}/respond",
                    json={"text": "it is sunny"},
                )
                assert r.status_code == 200

                result = await asyncio.wait_for(call, timeout=5)
                assert result.content[0].text == "it is sunny"
                assert result.is_error is False


@pytest.mark.asyncio
async def test_preset_response(base_url):
    async with httpx.AsyncClient(base_url=base_url) as rest:
        await rest.put("/api/tools", json={"tools": [{"name": "ping"}]})

        async with streamable_http_client(f"{base_url}/mcp") as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                await session.list_tools()
                sid = await _first_session_id(rest)

                await rest.put(
                    f"/api/sessions/{sid}/tools/ping/preset",
                    json={"text": "pong", "is_error": False},
                )

                # With a preset, the call returns immediately (no /respond).
                result = await asyncio.wait_for(
                    session.call_tool("ping", {}), timeout=5
                )
                assert result.content[0].text == "pong"


@pytest.mark.asyncio
async def test_timeout(base_url):
    async with httpx.AsyncClient(base_url=base_url) as rest:
        await rest.put("/api/tools", json={"tools": [{"name": "slow"}]})

        async with streamable_http_client(f"{base_url}/mcp") as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                await session.list_tools()
                sid = await _first_session_id(rest)
                await rest.put(f"/api/sessions/{sid}/mode", json={"mode": "manual"})

                # Never respond; default_timeout_s is 5s in the fixture.
                result = await asyncio.wait_for(
                    session.call_tool("slow", {}), timeout=15
                )
                assert result.is_error is True
                assert "timed out" in result.content[0].text

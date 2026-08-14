import httpx
import pytest

from mcp_explorer.app import create_app
from mcp_explorer.config import Settings


@pytest.fixture
def client():
    app = create_app(Settings(default_timeout_s=2.0))
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.asyncio
async def test_healthz(client):
    async with client:
        r = await client.get("/api/healthz")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_live_dashboard_served(client):
    async with client:
        r = await client.get("/live")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/html")
        assert "mcp-explorer" in r.text
        assert "/api/events" in r.text  # the SSE wiring is present


@pytest.mark.asyncio
async def test_set_default_tools_and_list_sessions(client):
    async with client:
        r = await client.put(
            "/api/tools",
            json={"tools": [{"name": "echo", "description": "echoes"}]},
        )
        assert r.status_code == 200
        assert r.json() == {"count": 1}

        r = await client.get("/api/sessions")
        assert r.status_code == 200
        assert r.json() == []


@pytest.mark.asyncio
async def test_unknown_session_404(client):
    async with client:
        r = await client.get("/api/sessions/nope")
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_respond_to_unknown_call_404(client):
    async with client:
        r = await client.post("/api/calls/deadbeef/respond", json={"text": "hi"})
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_api_token_guard():
    app = create_app(Settings(api_token="secret"))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        # health is unguarded
        assert (await c.get("/api/healthz")).status_code == 200
        # sessions is guarded
        assert (await c.get("/api/sessions")).status_code == 401
        r = await c.get("/api/sessions", headers={"Authorization": "Bearer secret"})
        assert r.status_code == 200

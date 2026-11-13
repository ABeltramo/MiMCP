"""REST control-plane contract tests (no MCP traffic)."""

from __future__ import annotations

import httpx
import pytest

from mimcp.app import create_app
from mimcp.config import Settings


@pytest.fixture
def client():
    app = create_app(Settings())
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.asyncio
async def test_healthz(client):
    async with client:
        r = await client.get("/api/healthz")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_create_session_returns_token_and_target(client):
    async with client:
        r = await client.post(
            "/api/sessions",
            json={
                "name": "agent-under-test",
                "instructions": "You are being tested.",
                "tools": [
                    {
                        "name": "echo",
                        "input_schema": {
                            "type": "object",
                            "properties": {"msg": {"type": "string"}},
                        },
                        "behavior": {
                            "type": "override",
                            "response": {"text": "canned"},
                        },
                    }
                ],
            },
        )
        assert r.status_code == 201
        body = r.json()
        assert body["name"] == "agent-under-test"
        assert body["status"] == "pending"
        assert body["instructions"] == "You are being tested."
        assert body["mcp"]["url"] == "http://test/mcp"
        assert body["mcp"]["headers"]["Authorization"] == f"Bearer {body['session_id']}"

        # The session shows up in the list and by id.
        assert any(
            s["session_id"] == body["session_id"] for s in (await client.get("/api/sessions")).json()
        )
        r = await client.get(f"/api/sessions/{body['session_id']}")
        assert r.status_code == 200
        assert r.json()["call_count"] == 0


@pytest.mark.asyncio
async def test_tool_validation_errors(client):
    async with client:
        # Synthetic tool without input_schema (no upstream on the session).
        r = await client.post(
            "/api/sessions", json={"tools": [{"name": "needs-schema"}]}
        )
        assert r.status_code == 422
        assert "input_schema" in r.json()["detail"]

        # Override without a response.
        r = await client.post(
            "/api/sessions",
            json={
                "tools": [
                    {
                        "name": "echo",
                        "input_schema": {},
                        "behavior": {"type": "override"},
                    }
                ]
            },
        )
        assert r.status_code == 422
        assert "response" in r.json()["detail"]

        # Inject without a template.
        r = await client.post(
            "/api/sessions",
            json={
                "tools": [
                    {
                        "name": "echo",
                        "input_schema": {},
                        "behavior": {"type": "inject"},
                    }
                ]
            },
        )
        assert r.status_code == 422
        assert "template" in r.json()["detail"]


@pytest.mark.asyncio
async def test_instructions_cannot_change_after_connect():
    app = create_app(Settings(instructions="global default"))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # There are no instructions endpoints: instructions are set at
        # session creation (POST /api/sessions) or via MIMCP_INSTRUCTIONS.
        assert (await client.get("/api/instructions")).status_code == 404
        sid = (await client.post("/api/sessions", json={})).json()["session_id"]
        assert (await client.get(f"/api/sessions/{sid}/instructions")).status_code == 404


@pytest.mark.asyncio
async def test_unknown_session_404(client):
    async with client:
        assert (await client.get("/api/sessions/nope")).status_code == 404
        assert (await client.get("/api/sessions/nope/tools")).status_code == 404
        assert (await client.get("/api/sessions/nope/calls")).status_code == 404


@pytest.mark.asyncio
async def test_calls_endpoints(client):
    async with client:
        sid = (await client.post("/api/sessions", json={})).json()["session_id"]
        assert (await client.get(f"/api/sessions/{sid}/calls")).json() == []
        r = await client.delete(f"/api/sessions/{sid}/calls")
        assert r.json() == {"session_id": sid, "cleared": 0}


@pytest.mark.asyncio
async def test_delete_session(client):
    async with client:
        sid = (await client.post("/api/sessions", json={})).json()["session_id"]
        assert (await client.delete(f"/api/sessions/{sid}")).status_code == 200
        assert (await client.get(f"/api/sessions/{sid}")).status_code == 404


@pytest.mark.asyncio
async def test_mcp_mount_rejects_missing_or_unknown_token():
    app = create_app(Settings())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=True
    ) as client:
        r = await client.post("/mcp", json={"jsonrpc": "2.0", "method": "initialize"})
        assert r.status_code == 401

        r = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "initialize"},
            headers={"X-MiMCP-Session": "not-a-real-token"},
        )
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_api_token_guard():
    app = create_app(Settings(api_token="secret"))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        # health is unguarded
        assert (await c.get("/api/healthz")).status_code == 200
        # sessions are guarded
        assert (await c.get("/api/sessions")).status_code == 401
        r = await c.get("/api/sessions", headers={"Authorization": "Bearer secret"})
        assert r.status_code == 200

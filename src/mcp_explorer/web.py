"""Serves the tiny vanilla-JS dashboard at /live.

The page is a single self-contained HTML file (no frameworks, no CDN) that talks
to the existing /api REST + SSE endpoints. It is read at request time so it can
be hot-edited without restarting the server.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse

_LIVE_HTML = Path(__file__).parent / "static" / "live.html"


def create_web_router() -> APIRouter:
    router = APIRouter()

    @router.get("/live", response_class=HTMLResponse)
    async def live() -> HTMLResponse:
        try:
            return HTMLResponse(_LIVE_HTML.read_text(encoding="utf-8"))
        except FileNotFoundError:  # pragma: no cover
            raise HTTPException(status_code=500, detail="live.html not found")

    return router

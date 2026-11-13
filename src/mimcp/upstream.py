"""Upstream MCP connections: one supervised ClientSession per session.

The SDK client transports are async context managers that spawn their tasks in
the task that enters them, but REST handlers and MCP handlers each run in their
own tasks. Each `UpstreamConnection` therefore owns a supervisor task that:

1. enters ``streamable_http_client`` / ``ClientSession`` via an AsyncExitStack
   (performing the upstream initialize handshake),
2. executes upstream requests serially on behalf of other tasks, through a
   queue of futures,
3. reconnects with backoff when the transport fails, and exits cleanly on stop.

All supervisor tasks live in a task group started inside the app lifespan.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from typing import Any, TypeVar

import anyio.abc
import mcp.types as types
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

from .config import Settings

logger = logging.getLogger(__name__)
T = TypeVar("T")

_RECONNECT_DELAY_S = 1.0


class UpstreamError(RuntimeError):
    """An upstream request failed or the upstream is unreachable."""


class _Request:
    """One queued upstream operation plus the future its caller awaits."""

    __slots__ = ("fn", "future")

    def __init__(
        self, fn: Callable[[ClientSession], Awaitable[Any]], future: "asyncio.Future[Any]"
    ) -> None:
        self.fn = fn
        self.future = future


class UpstreamConnection:
    """A reconnecting streamable-HTTP client connection to one upstream."""

    def __init__(
        self,
        session_id: str,
        url: str,
        headers: dict[str, str],
        settings: Settings,
    ) -> None:
        self.session_id = session_id
        self.url = url
        self.headers = dict(headers)
        self._settings = settings
        self._queue: asyncio.Queue[_Request] = asyncio.Queue()
        self._stop = asyncio.Event()
        self._ready = asyncio.Event()
        self._done = asyncio.Event()
        self.connected = False
        self.last_error: str | None = None

    # --- lifecycle --------------------------------------------------------

    def start(self, task_group: anyio.abc.TaskGroup) -> None:
        task_group.start_soon(self._supervise)

    async def stop(self) -> None:
        self._stop.set()
        await self._done.wait()

    async def wait_ready(self, timeout_s: float | None = None) -> None:
        """Block until the upstream handshake completed (or raise)."""
        timeout = self._settings.upstream_connect_timeout_s if timeout_s is None else timeout_s
        try:
            await asyncio.wait_for(self._ready.wait(), timeout)
        except asyncio.TimeoutError:
            detail = f"upstream {self.url} not ready after {timeout}s"
            if self.last_error:
                detail += f" (last error: {self.last_error})"
            raise UpstreamError(detail) from None

    # --- upstream operations ---------------------------------------------

    async def list_tools(self) -> list[types.Tool]:
        result = await self._request(lambda session: session.list_tools())
        return list(result.tools)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        return await self._request(
            lambda session: session.call_tool(name, arguments=arguments)
        )

    async def _request(self, fn: Callable[[ClientSession], Awaitable[T]]) -> T:
        if self._stop.is_set():
            raise UpstreamError(
                f"upstream connection for session '{self.session_id}' is stopped"
            )
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        await self._queue.put(_Request(fn, future))
        # Bounded wait: a mid-call reconnect must not hang the caller forever.
        try:
            return await asyncio.wait_for(future, self._settings.upstream_timeout_s)
        except asyncio.TimeoutError:
            raise UpstreamError(
                f"upstream {self.url} did not answer within "
                f"{self._settings.upstream_timeout_s}s"
            ) from None

    # --- supervisor -------------------------------------------------------

    async def _supervise(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    await self._connect_and_serve()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.last_error = str(exc) or exc.__class__.__name__
                    logger.warning(
                        "upstream %s (session %s) failed: %s",
                        self.url,
                        self.session_id,
                        self.last_error,
                    )
                if not self._stop.is_set():
                    await asyncio.sleep(_RECONNECT_DELAY_S)
        finally:
            self.connected = False
            self._ready.clear()
            self._fail_pending()
            self._done.set()

    async def _connect_and_serve(self) -> None:
        async with AsyncExitStack() as stack:
            http = create_mcp_http_client(headers=self.headers)
            await stack.enter_async_context(http)
            # terminate_on_close sends DELETE to the upstream on exit, so the
            # upstream session is cleaned up when we stop or reconnect.
            read_stream, write_stream = await stack.enter_async_context(
                streamable_http_client(self.url, http_client=http)
            )
            session = await stack.enter_async_context(
                ClientSession(read_stream, write_stream)
            )
            await session.initialize()
            self.connected = True
            self.last_error = None
            self._ready.set()
            logger.info(
                "upstream %s connected for session %s", self.url, self.session_id
            )
            await self._serve(session)

    async def _serve(self, session: ClientSession) -> None:
        stop_task = asyncio.create_task(self._stop.wait())
        try:
            while True:
                get_task = asyncio.create_task(self._queue.get())
                done, _ = await asyncio.wait(
                    {get_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
                )
                if stop_task in done:
                    get_task.cancel()
                    return
                request = get_task.result()
                try:
                    result = await asyncio.wait_for(
                        request.fn(session), self._settings.upstream_timeout_s
                    )
                except Exception as exc:
                    if not request.future.done():
                        request.future.set_exception(UpstreamError(f"upstream call failed: {exc}"))
                    # The connection is suspect: drop it and reconnect.
                    raise
                if not request.future.done():
                    request.future.set_result(result)
        finally:
            stop_task.cancel()

    def _fail_pending(self) -> None:
        while True:
            try:
                request = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            if not request.future.done():
                request.future.set_exception(
                    UpstreamError(f"upstream {self.url} connection stopped")
                )


class UpstreamManager:
    """Tracks the per-session upstream connections."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._connections: dict[str, UpstreamConnection] = {}
        self._task_group: anyio.abc.TaskGroup | None = None

    def start(self, task_group: anyio.abc.TaskGroup) -> None:
        self._task_group = task_group

    def connect(self, session_id: str, url: str, headers: dict[str, str]) -> UpstreamConnection:
        """Start a connection for a session.

        Callers must `await disconnect(session_id)` first when switching
        upstreams, otherwise the previous supervisor task leaks.
        """
        if self._task_group is None:
            raise RuntimeError(
                "upstream manager not started; the app lifespan must run"
            )
        connection = UpstreamConnection(session_id, url, headers, self._settings)
        connection.start(self._task_group)
        self._connections[session_id] = connection
        return connection

    async def disconnect(self, session_id: str) -> None:
        connection = self._connections.pop(session_id, None)
        if connection is not None:
            await connection.stop()

    def get(self, session_id: str) -> UpstreamConnection | None:
        return self._connections.get(session_id)

    async def shutdown(self) -> None:
        for session_id in list(self._connections):
            await self.disconnect(session_id)

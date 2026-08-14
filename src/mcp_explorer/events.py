"""In-process pub/sub event bus feeding the SSE stream."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator

from .models import Event


class EventBus:
    """Fan-out bus: every published event is delivered to all subscribers.

    Subscribers are plain asyncio queues; slow or dead subscribers are bounded
    by `maxsize` and simply drop the oldest events rather than blocking the
    publisher (which is on the hot path of tool calls).
    """

    def __init__(self, maxsize: int = 1000) -> None:
        self._subscribers: set[asyncio.Queue[Event]] = set()
        self._maxsize = maxsize

    def publish(self, event: Event) -> None:
        for queue in self._subscribers:
            if queue.full():
                # Drop oldest to make room; never block the caller.
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:  # pragma: no cover
                    pass
            queue.put_nowait(event)

    def emit(self, type_: str, session_id: str | None = None, **data) -> None:
        """Convenience wrapper: build an Event and publish it."""
        self.publish(
            Event(
                type=type_,  # type: ignore[arg-type]
                session_id=session_id,
                timestamp=time.time(),
                data=data,
            )
        )

    async def subscribe(
        self, session_id: str | None = None
    ) -> AsyncIterator[Event]:
        """Yield events until the consumer disconnects.

        If `session_id` is given, only events for that session (plus global
        events with no session_id) are yielded.
        """
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=self._maxsize)
        self._subscribers.add(queue)
        try:
            while True:
                event = await queue.get()
                if session_id is not None and event.session_id not in (
                    None,
                    session_id,
                ):
                    continue
                yield event
        finally:
            self._subscribers.discard(queue)

"""Shared per-session event bus used by drivers.

One queue feeds both ``stream_events()`` and the filtered ``send_turn()`` view.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

from talktoharnesses.events import RuntimeEvent


class EventBus:
    """Fan-out bus: publish once, every subscriber receives every event."""

    def __init__(self) -> None:
        self._subscribers: list[asyncio.Queue[RuntimeEvent | None]] = []
        self._closed = False

    def subscribe(self) -> asyncio.Queue[RuntimeEvent | None]:
        q: asyncio.Queue[RuntimeEvent | None] = asyncio.Queue()
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[RuntimeEvent | None]) -> None:
        with contextlib.suppress(ValueError):
            self._subscribers.remove(q)

    def publish(self, event: RuntimeEvent) -> None:
        if self._closed:
            return
        for q in list(self._subscribers):
            q.put_nowait(event)

    def close(self) -> None:
        """Wake all subscribers with a sentinel ``None``."""
        self._closed = True
        for q in list(self._subscribers):
            q.put_nowait(None)

    async def iter_queue(
        self,
        q: asyncio.Queue[RuntimeEvent | None],
    ) -> AsyncIterator[RuntimeEvent]:
        try:
            while True:
                item = await q.get()
                if item is None:
                    break
                yield item
        finally:
            self.unsubscribe(q)

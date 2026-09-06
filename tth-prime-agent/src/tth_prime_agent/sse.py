"""SSE frame streaming for a session's queue: typed frames + keepalives."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Coroutine

from tth_types.split_api import FRAME_END

from tth_prime_agent.sessions import SessionEntry

_KEEPALIVE_INTERVAL_S = 15.0


async def _frames(entry: SessionEntry) -> AsyncIterator[bytes]:
    """Yield SSE-encoded frames until the end frame or queue sentinel."""
    while True:
        try:
            item = await asyncio.wait_for(entry.queue.get(), timeout=_KEEPALIVE_INTERVAL_S)
        except TimeoutError:
            yield b": keepalive\n\n"
            continue
        if item is None:
            return
        event_name, data_json = item
        frame_id = entry.next_frame_id
        entry.next_frame_id += 1
        yield f"event: {event_name}\nid: {frame_id}\ndata: {data_json}\n\n".encode()
        if event_name == FRAME_END:
            return


class SessionFrameStream:
    """Async frame iterator with a synchronous Django response closer."""

    def __init__(
        self, entry: SessionEntry, close_session: Callable[[], Coroutine[object, object, None]]
    ) -> None:
        self._iterator = _frames(entry)
        self._close_session = close_session
        self._loop = asyncio.get_running_loop()
        self._close_task: asyncio.Task[None] | None = None

    def __aiter__(self) -> SessionFrameStream:
        return self

    async def __anext__(self) -> bytes:
        return await anext(self._iterator)

    def close(self) -> None:
        self._loop.call_soon_threadsafe(self._schedule_close)

    def _schedule_close(self) -> None:
        if self._close_task is None:
            self._close_task = self._loop.create_task(self._run_close())

    async def _run_close(self) -> None:
        # create_task wants a coroutine, not a bare Awaitable.
        await self._close_session()


def stream_frames(
    entry: SessionEntry, close_session: Callable[[], Coroutine[object, object, None]]
) -> SessionFrameStream:
    return SessionFrameStream(entry, close_session)

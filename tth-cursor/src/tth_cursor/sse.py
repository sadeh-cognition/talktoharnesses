"""SSE frame streaming for a session's queue: typed frames + keepalives."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Coroutine

from tth_types.split_api import FRAME_END

from tth_cursor.sessions import Frame, SessionEntry

_KEEPALIVE_INTERVAL_S = 15.0


def _encode(frame: Frame) -> bytes:
    return f"event: {frame.event}\nid: {frame.id}\ndata: {frame.data}\n\n".encode()


async def _frames(entry: SessionEntry, replay: list[Frame]) -> AsyncIterator[bytes]:
    """Yield SSE-encoded frames until the end frame or queue sentinel.

    A reattaching subscriber first gets the retained frames after its cursor.
    """
    for frame in replay:
        yield _encode(frame)
        if frame.event == FRAME_END:
            return
    while True:
        try:
            item = await asyncio.wait_for(entry.queue.get(), timeout=_KEEPALIVE_INTERVAL_S)
        except TimeoutError:
            yield b": keepalive\n\n"
            continue
        if item is None:
            return
        # Retained before it is written: a subscriber that drops mid-write
        # gets it again on reattach, and its cursor dedupes the repeat.
        entry.sent(item)
        yield _encode(item)
        if item.event == FRAME_END:
            return


class SessionFrameStream:
    """Async frame iterator with a synchronous Django response closer."""

    def __init__(
        self,
        entry: SessionEntry,
        close_session: Callable[[], Coroutine[object, object, None]],
        *,
        replay: list[Frame],
    ) -> None:
        self._iterator = _frames(entry, replay)
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
    entry: SessionEntry,
    close_session: Callable[[], Coroutine[object, object, None]],
    *,
    replay: list[Frame],
) -> SessionFrameStream:
    return SessionFrameStream(entry, close_session, replay=replay)

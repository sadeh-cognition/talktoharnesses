"""Remote mirror of the supervised-process surface RuntimeManager relies on.

Fed by ``process`` SSE frames from a split service; termination calls go back
over HTTP. The mirrored surface matches ``runtime.handle.ProcessHandle`` where
the manager reads it: pid/returncode/stderr-tail/forced flags, ``events()``,
``close()``, and ``force_terminate()``.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable

from tth_types.process import ProcessEvent
from tth_types.split_api import ProcessFrame, ProcessSnapshot

TerminateCall = Callable[[str | None], Awaitable[None]]


class RemoteProcessHandle:
    """Read-side mirror of a split-supervised process."""

    def __init__(self, *, pid: int | None, terminate: TerminateCall) -> None:
        self._snapshot = ProcessSnapshot(pid=pid)
        self._terminate = terminate
        self._queue: asyncio.Queue[ProcessEvent | None] = asyncio.Queue()
        self._stream_closed = False
        self._forced_locally = False
        self._forced_reason_local: str | None = None

    @property
    def pid(self) -> int | None:
        return self._snapshot.pid

    @property
    def returncode(self) -> int | None:
        return self._snapshot.returncode

    @property
    def redacted_stderr_tail(self) -> str:
        return self._snapshot.redacted_stderr_tail

    @property
    def forced(self) -> bool:
        return self._snapshot.forced or self._forced_locally

    @property
    def forced_reason(self) -> str | None:
        return self._snapshot.forced_reason or self._forced_reason_local

    @property
    def stderr_truncated(self) -> bool:
        return self._snapshot.stderr_truncated

    @property
    def retained_stderr_bytes(self) -> int:
        return self._snapshot.retained_stderr_bytes

    def on_frame(self, frame: ProcessFrame) -> None:
        """Apply one process SSE frame: refresh the snapshot, queue the event."""
        self._snapshot = frame.snapshot
        if not self._stream_closed:
            self._queue.put_nowait(frame.event)

    def mark_stream_closed(self) -> None:
        if self._stream_closed:
            return
        self._stream_closed = True
        self._queue.put_nowait(None)

    def events(self) -> AsyncIterator[ProcessEvent]:
        async def _gen() -> AsyncIterator[ProcessEvent]:
            while True:
                item = await self._queue.get()
                if item is None:
                    return
                yield item

        return _gen()

    async def close(self) -> None:
        """No-op: the adapter's session close already settles the remote process."""
        return

    async def force_terminate(self, *, reason: str | None = "forced") -> None:
        # Record the intent locally so the manager's terminal persistence sees a
        # forced termination even when the confirming frame has not arrived yet.
        self._forced_locally = True
        if self._forced_reason_local is None:
            self._forced_reason_local = reason
        with contextlib.suppress(Exception):
            await self._terminate(reason)

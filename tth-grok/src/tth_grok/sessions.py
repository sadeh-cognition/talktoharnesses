"""In-memory session store: one adapter, optional supervised process, one stream.

Sessions do not survive a service restart — the proxy detects the dropped SSE
stream and recovers through native session resume against a fresh session.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal, cast
from uuid import UUID, uuid4

from tth_types.adapter import HarnessAdapter, HarnessInteractionRequest, HarnessSession
from tth_types.enums import ErrorCode
from tth_types.errors import DomainError
from tth_types.harness import LaunchSnapshot
from tth_types.split_api import (
    FRAME_END,
    FRAME_HARNESS_EVENT,
    FRAME_INTERACTION,
    FRAME_PROCESS,
    EndFrame,
    HarnessEventFrame,
    InteractionFrame,
    ProcessFrame,
    ProcessSnapshot,
)

from tth_grok.runtime.handle import ProcessHandle
from tth_grok.shared.policy import RuntimePolicy

logger = logging.getLogger(__name__)

# Bounded so a proxy that never drains cannot grow memory without limit; the
# pumps block on a full queue, which backpressures the harness stream.
_FRAME_QUEUE_MAXSIZE = 4096

SeenExport = Callable[[], tuple[frozenset[str], frozenset[str]]]


def _export_seen(adapter: HarnessAdapter) -> tuple[frozenset[str], frozenset[str]]:
    export = getattr(adapter, "export_seen", None)
    if callable(export):
        return cast(SeenExport, export)()
    return frozenset(), frozenset()


def process_snapshot(handle: ProcessHandle) -> ProcessSnapshot:
    return ProcessSnapshot(
        pid=handle.pid,
        returncode=handle.returncode,
        redacted_stderr_tail=handle.redacted_stderr_tail,
        forced=handle.forced,
        forced_reason=handle.forced_reason,
        stderr_truncated=handle.stderr_truncated,
        retained_stderr_bytes=handle.retained_stderr_bytes,
    )


@dataclass
class SessionEntry:
    session_id: UUID
    adapter: HarnessAdapter
    session: HarnessSession
    launch: LaunchSnapshot
    handle: ProcessHandle | None = None
    queue: asyncio.Queue[tuple[str, str] | None] = field(
        default_factory=lambda: asyncio.Queue(maxsize=_FRAME_QUEUE_MAXSIZE)
    )
    pump_task: asyncio.Task[None] | None = None
    process_pump_task: asyncio.Task[None] | None = None
    next_frame_id: int = 1
    stream_attached: bool = False
    closed: bool = False

    async def enqueue(self, event_name: str, data_json: str) -> None:
        await self.queue.put((event_name, data_json))


class SessionStore:
    def __init__(self, *, policy: RuntimePolicy | None = None) -> None:
        self._policy = policy or RuntimePolicy()
        self._entries: dict[UUID, SessionEntry] = {}

    def __len__(self) -> int:
        return len(self._entries)

    def add(
        self,
        adapter: HarnessAdapter,
        session: HarnessSession,
        launch: LaunchSnapshot,
        *,
        handle: ProcessHandle | None = None,
        session_id: UUID | None = None,
    ) -> SessionEntry:
        if len(self._entries) >= self._policy.max_runtimes:
            raise DomainError(
                ErrorCode.CONVERSATION_BUSY,
                "split session capacity reached",
                details={"max_sessions": self._policy.max_runtimes},
            )
        entry = SessionEntry(
            session_id=session_id or uuid4(),
            adapter=adapter,
            session=session,
            launch=launch,
            handle=handle,
        )
        self._entries[entry.session_id] = entry
        loop = asyncio.get_running_loop()
        entry.pump_task = loop.create_task(
            self._pump(entry),
            name=f"split-pump-{entry.session_id}",
        )
        if handle is not None:
            entry.process_pump_task = loop.create_task(
                self._process_pump(entry, handle),
                name=f"split-process-pump-{entry.session_id}",
            )
        return entry

    def attach_stream(self, session_id: UUID) -> SessionEntry:
        entry = self.get(session_id)
        if entry.stream_attached:
            raise DomainError(
                ErrorCode.CONVERSATION_BUSY,
                "split session already has an event subscriber",
                details={"session_id": str(session_id)},
            )
        entry.stream_attached = True
        return entry

    async def close_binding(self, conversation_id: UUID, binding_id: UUID) -> None:
        for entry in tuple(self._entries.values()):
            if (
                entry.session.conversation_id == conversation_id
                and entry.session.binding_id == binding_id
            ):
                await self.close(entry.session_id, reason="terminated")

    def get(self, session_id: UUID) -> SessionEntry:
        entry = self._entries.get(session_id)
        if entry is None:
            raise DomainError(
                ErrorCode.NOT_FOUND,
                "split session not found",
                details={"session_id": str(session_id)},
            )
        return entry

    async def close(
        self,
        session_id: UUID,
        *,
        reason: Literal["closed", "stream_ended", "terminated"] = "closed",
    ) -> None:
        entry = self._entries.pop(session_id, None)
        if entry is None or entry.closed:
            return
        entry.closed = True
        try:
            await asyncio.wait_for(
                entry.adapter.close(entry.session),
                timeout=self._policy.graceful_close_timeout,
            )
        except (TimeoutError, Exception):  # noqa: BLE001
            if entry.handle is not None:
                with contextlib.suppress(Exception):
                    await entry.handle.force_terminate(reason="graceful_close_timeout")
        else:
            if entry.handle is not None:
                with contextlib.suppress(Exception):
                    await entry.handle.close()
        tasks = [task for task in (entry.pump_task, entry.process_pump_task) if task is not None]
        if tasks:
            _, pending = await asyncio.wait(
                tasks,
                timeout=0 if entry.queue.full() else self._policy.graceful_close_timeout,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        drop_count = (
            max(0, entry.queue.qsize() + 2 - entry.queue.maxsize) if entry.queue.maxsize > 0 else 0
        )
        for _ in range(drop_count):
            entry.queue.get_nowait()
        entry.queue.put_nowait((FRAME_END, EndFrame(reason=reason).model_dump_json()))
        entry.queue.put_nowait(None)

    async def terminate(self, session_id: UUID, *, reason: str | None) -> None:
        """Force-kill the supervised process, then settle the session."""
        entry = self._entries.get(session_id)
        if entry is None:
            raise DomainError(
                ErrorCode.NOT_FOUND,
                "split session not found",
                details={"session_id": str(session_id)},
            )
        if entry.handle is not None:
            with contextlib.suppress(Exception):
                await entry.handle.force_terminate(reason=reason or "forced")
        await self.close(session_id, reason="terminated")

    async def shutdown(self) -> None:
        for session_id in list(self._entries):
            await self.close(session_id, reason="terminated")

    async def _pump(self, entry: SessionEntry) -> None:
        """Forward normalized harness output as SSE frames with dedupe deltas."""
        before_ids, before_offsets = _export_seen(entry.adapter)
        try:
            async for item in entry.adapter.events(entry.session):
                after_ids, after_offsets = _export_seen(entry.adapter)
                new_ids = tuple(sorted(after_ids - before_ids))
                new_offsets = tuple(sorted(after_offsets - before_offsets))
                before_ids, before_offsets = after_ids, after_offsets
                if isinstance(item, HarnessInteractionRequest):
                    frame_json = InteractionFrame(
                        payload=item.payload,
                        provider_correlation=item.provider_correlation,
                        new_native_ids=new_ids,
                        new_stream_offsets=new_offsets,
                    ).model_dump_json()
                    await entry.enqueue(FRAME_INTERACTION, frame_json)
                else:
                    frame_json = HarnessEventFrame(
                        item=item,
                        new_native_ids=new_ids,
                        new_stream_offsets=new_offsets,
                    ).model_dump_json()
                    await entry.enqueue(FRAME_HARNESS_EVENT, frame_json)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("session pump failed for %s", entry.session_id)
        if not entry.closed:
            await entry.enqueue(FRAME_END, EndFrame(reason="stream_ended").model_dump_json())
            await entry.queue.put(None)

    async def _process_pump(self, entry: SessionEntry, handle: ProcessHandle) -> None:
        """Forward supervised-process lifecycle events with fresh snapshots."""
        try:
            async for event in handle.events():
                frame = ProcessFrame(event=event, snapshot=process_snapshot(handle))
                await entry.enqueue(FRAME_PROCESS, frame.model_dump_json())
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("process pump failed for %s", entry.session_id)


_store: SessionStore | None = None


def get_session_store() -> SessionStore:
    global _store
    if _store is None:
        _store = SessionStore()
    return _store


def reset_session_store_for_tests() -> None:
    global _store
    _store = None

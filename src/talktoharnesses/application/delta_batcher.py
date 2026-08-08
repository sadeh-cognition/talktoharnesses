"""50 ms delta accumulator for harness event projection commits."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from talktoharnesses.domain.events import ConversationEvent
from talktoharnesses.domain.models import Command
from talktoharnesses.domain.transitions import ConversationState

logger = logging.getLogger(__name__)

FlushFn = Callable[
    [int, ConversationState, Sequence[ConversationEvent], Sequence[Command]],
    Awaitable[Sequence[ConversationEvent]],
]


@dataclass
class _Pending:
    base_version: int | None = None
    state: ConversationState | None = None
    events: list[ConversationEvent] = field(default_factory=lambda: [])
    commands: list[Command] = field(default_factory=lambda: [])


class DeltaBatcher:
    """Accumulate events and flush atomically after a short delay or force."""

    def __init__(
        self,
        *,
        conversation_id: UUID,
        flush: FlushFn,
        interval_ms: float = 50.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._conversation_id = conversation_id
        self._flush = flush
        self._interval = interval_ms / 1000.0
        self._clock = clock or (lambda: datetime.now(UTC))
        self._pending = _Pending()
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._closed = False

    @property
    def state(self) -> ConversationState | None:
        return self._pending.state

    async def add(
        self,
        *,
        base_version: int,
        state: ConversationState,
        events: Sequence[ConversationEvent],
        commands: Sequence[Command] = (),
        force: bool = False,
    ) -> Sequence[ConversationEvent]:
        """Queue a transition result; force=True flushes immediately.

        ``base_version`` is the aggregate version *before* the first event in
        this pending batch (the optimistic concurrency token for commit).
        """
        async with self._lock:
            if self._closed:
                return ()
            if self._pending.base_version is None:
                self._pending.base_version = base_version
            self._pending.state = state
            self._pending.events.extend(events)
            self._pending.commands.extend(commands)
            if force or self._interval <= 0:
                return await self._do_flush()
            if self._task is None or self._task.done():
                self._task = asyncio.create_task(self._delayed_flush(), name="delta-batcher")
            return ()

    async def flush(self) -> Sequence[ConversationEvent]:
        async with self._lock:
            return await self._do_flush()

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
            if self._task is not None and not self._task.done():
                self._task.cancel()
            await self._do_flush()

    async def _delayed_flush(self) -> None:
        try:
            await asyncio.sleep(self._interval)
            async with self._lock:
                await self._do_flush()
        except asyncio.CancelledError:
            return

    async def _do_flush(self) -> Sequence[ConversationEvent]:
        if self._pending.state is None or self._pending.base_version is None:
            return ()
        if not self._pending.events and not self._pending.commands:
            return ()
        state = self._pending.state
        events = tuple(self._pending.events)
        commands = tuple(self._pending.commands)
        base_version = self._pending.base_version
        committed = await self._flush(base_version, state, events, commands)
        self._pending = _Pending()
        return committed

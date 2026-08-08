"""In-process committed-event broker (SQLite profile and base for Django)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Sequence
from uuid import UUID

from talktoharnesses.application.publisher import ConversationWakeup
from talktoharnesses.domain.events import ConversationEvent

logger = logging.getLogger(__name__)

# Fixed internal timings (Phase 5) — not public settings.
SQLITE_POLL_INTERVAL_S = 0.25
KEEPALIVE_INTERVAL_S = 15.0


class _Subscriber:
    """One SSE/live consumer for a conversation; wakeups coalesce to highest seq."""

    __slots__ = ("event", "sequence", "closed")

    def __init__(self) -> None:
        self.event = asyncio.Event()
        self.sequence = 0
        self.closed = False

    def wake(self, sequence: int) -> None:
        if self.closed:
            return
        if sequence > self.sequence:
            self.sequence = sequence
        self.event.set()

    def close(self) -> None:
        self.closed = True
        self.event.set()


class InProcessCommittedEventBroker:
    """Process-local broker: publish fans out wakeups; subscribers coalesce sequences.

    Suitable as the SQLite backend and as the in-process fan-out layer for
    PostgreSQL (with optional NOTIFY/LISTEN layered on top).
    """

    def __init__(
        self,
        *,
        poll_interval: float = SQLITE_POLL_INTERVAL_S,
        keepalive_interval: float = KEEPALIVE_INTERVAL_S,
    ) -> None:
        self._poll_interval = poll_interval
        self._keepalive_interval = keepalive_interval
        self._subs: dict[UUID, set[_Subscriber]] = {}
        self._high_water: dict[UUID, int] = {}
        self._lock = asyncio.Lock()
        self._started = False
        self._poll_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        # Background poll is a no-op fan-out of high-water for open subscriptions
        # (covers missed in-process signals under the single-supervisor profile).
        self._poll_task = asyncio.create_task(self._poll_loop(), name="event-broker-poll")

    async def stop(self) -> None:
        self._started = False
        if self._poll_task is not None:
            self._poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._poll_task
            self._poll_task = None
        async with self._lock:
            for subs in self._subs.values():
                for sub in list(subs):
                    sub.close()
            self._subs.clear()

    async def publish(self, events: Sequence[ConversationEvent]) -> None:
        if not events:
            return
        by_conversation: dict[UUID, int] = {}
        for event in events:
            cid = event.conversation_id
            prev = by_conversation.get(cid, 0)
            if event.sequence > prev:
                by_conversation[cid] = event.sequence
        for conversation_id, sequence in by_conversation.items():
            await self.notify(conversation_id, sequence)

    async def notify(self, conversation_id: UUID, sequence: int) -> None:
        """Wake subscribers with a coalesced high-water sequence (no event bodies)."""
        async with self._lock:
            current = self._high_water.get(conversation_id, 0)
            if sequence > current:
                self._high_water[conversation_id] = sequence
            high = self._high_water.get(conversation_id, sequence)
            subscribers = list(self._subs.get(conversation_id, ()))
        for sub in subscribers:
            sub.wake(high)

    def high_water(self, conversation_id: UUID) -> int:
        return self._high_water.get(conversation_id, 0)

    def subscribe(self, conversation_id: UUID) -> AsyncIterator[ConversationWakeup]:
        return self._subscribe(conversation_id)

    async def _subscribe(self, conversation_id: UUID) -> AsyncIterator[ConversationWakeup]:
        sub = _Subscriber()
        async with self._lock:
            self._subs.setdefault(conversation_id, set()).add(sub)
            # Seed with current high-water so a subscriber joining mid-stream
            # can immediately reconcile without waiting for the next publish.
            initial = self._high_water.get(conversation_id, 0)
        if initial > 0:
            sub.wake(initial)
        try:
            while not sub.closed:
                try:
                    await asyncio.wait_for(sub.event.wait(), timeout=self._keepalive_interval)
                except TimeoutError:
                    # Keepalive / reconcile tick (may carry stale high-water).
                    yield ConversationWakeup(
                        conversation_id=conversation_id,
                        sequence=self._high_water.get(conversation_id, 0),
                    )
                    continue
                sub.event.clear()
                if sub.closed:
                    break
                yield ConversationWakeup(
                    conversation_id=conversation_id,
                    sequence=sub.sequence,
                )
        finally:
            sub.close()
            async with self._lock:
                bucket = self._subs.get(conversation_id)
                if bucket is not None:
                    bucket.discard(sub)
                    if not bucket:
                        del self._subs[conversation_id]

    async def _poll_loop(self) -> None:
        while self._started:
            try:
                await asyncio.sleep(self._poll_interval)
                async with self._lock:
                    # Re-signal open subscriptions with known high-water so a
                    # missed Event.set cannot leave a consumer stuck forever.
                    snapshot = {
                        cid: (self._high_water.get(cid, 0), list(subs))
                        for cid, subs in self._subs.items()
                    }
                for _cid, (seq, subs) in snapshot.items():
                    if seq <= 0:
                        continue
                    for sub in subs:
                        sub.wake(seq)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("event broker poll loop failed")

"""Committed-event publication and broker protocols."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from talktoharnesses.domain.events import ConversationEvent


class CommittedEventPublisher(Protocol):
    """Publish events that have already been committed to durable storage.

    Only already-committed events may be passed to :meth:`publish`. Callers
    must never publish speculative or uncommitted event batches.
    """

    async def publish(self, events: Sequence[ConversationEvent]) -> None:
        """Notify live subscribers of committed conversation events."""
        ...


@dataclass(frozen=True, slots=True)
class ConversationWakeup:
    """Hint that a conversation may have new committed events.

    Contains no event bodies — consumers always read from persistence.
    ``sequence`` is the highest committed sequence known to the publisher
    (or 0 when used as a keepalive reconcile tick).
    """

    conversation_id: UUID
    sequence: int


class CommittedEventBroker(CommittedEventPublisher, Protocol):
    """Wakeup broker for SSE live delivery.

    Extends :class:`CommittedEventPublisher` with explicit lifecycle and
    per-conversation wakeup subscription. The Phase 4 command processor
    continues to depend only on :meth:`publish`.
    """

    async def start(self) -> None:
        """Start listener / poll tasks (idempotent)."""
        ...

    async def stop(self) -> None:
        """Stop listener / poll tasks and release resources (idempotent)."""
        ...

    def subscribe(self, conversation_id: UUID) -> AsyncIterator[ConversationWakeup]:
        """Yield wakeups for ``conversation_id`` until the iterator is closed/cancelled."""
        ...

"""Committed-event publication protocol."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from talktoharnesses.domain.events import ConversationEvent


class CommittedEventPublisher(Protocol):
    """Publish events that have already been committed to durable storage.

    Only already-committed events may be passed to :meth:`publish`. Callers
    must never publish speculative or uncommitted event batches.
    """

    async def publish(self, events: Sequence[ConversationEvent]) -> None:
        """Notify live subscribers of committed conversation events."""
        ...

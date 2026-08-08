"""Coarse asynchronous persistence protocols (business operations, not CRUD)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol
from uuid import UUID

from talktoharnesses.domain.events import ConversationEvent
from talktoharnesses.domain.models import Command, InteractionAnswer, LaunchSnapshot, ProcessRecord
from talktoharnesses.domain.transitions import ConversationState


class Persistence(Protocol):
    """Injected durable state boundary required for all execution paths.

    Implementations (e.g. Django) live in later phases. Callers must refuse
    to execute turns when no persistence implementation is configured
    (``ErrorCode.PERSISTENCE_REQUIRED``).
    """

    async def get_snapshot(self, conversation_id: UUID, owner_id: str) -> ConversationState:
        """Load an owner-scoped conversation aggregate snapshot."""
        ...

    async def save_snapshot(self, state: ConversationState) -> ConversationState:
        """Persist a snapshot using optimistic concurrency on ``version``."""
        ...

    async def accept_command(self, command: Command) -> Command:
        """Accept a command idempotently by ``(conversation_id, idempotency_key)``."""
        ...

    async def claim_commands(self, worker_id: str, limit: int) -> Sequence[Command]:
        """Claim accepted work for a worker under lease semantics."""
        ...

    async def update_command(self, command: Command) -> Command:
        """Update command delivery/settlement fields."""
        ...

    async def commit_event_batch(
        self,
        conversation_id: UUID,
        expected_version: int,
        state: ConversationState,
        events: Sequence[ConversationEvent],
    ) -> Sequence[ConversationEvent]:
        """Atomically persist projection state and conversation events."""
        ...

    async def commit_runtime_lifecycle(
        self,
        conversation_id: UUID,
        expected_version: int,
        state: ConversationState,
        process: ProcessRecord | None,
        launch_history_entry: LaunchSnapshot | None,
        events: Sequence[ConversationEvent],
    ) -> Sequence[ConversationEvent]:
        """Atomically update aggregate, process record/tail, launch history, events.

        Retries are idempotent by process-incarnation UUID on ``process.id``.
        Sequence allocation uses the same conversation-local scheme as
        ``commit_event_batch``.
        """
        ...

    async def replay(
        self,
        conversation_id: UUID,
        after_sequence: int,
        event_count_limit: int,
        byte_limit: int,
    ) -> Sequence[ConversationEvent]:
        """Replay committed events after ``after_sequence`` within limits."""
        ...

    async def resolve_interaction(
        self,
        interaction_id: UUID,
        answer: InteractionAnswer,
    ) -> InteractionAnswer:
        """Resolve an interaction with first-write-wins semantics."""
        ...

    async def delete_expired_turn_aggregates(self, cutoff: datetime) -> int:
        """Retention: delete complete expired turn aggregates. Returns count."""
        ...

    async def purge_soft_deleted(self, cutoff: datetime) -> int:
        """Retention: permanently purge soft-deleted conversations. Returns count."""
        ...

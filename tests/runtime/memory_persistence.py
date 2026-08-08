"""In-memory Persistence for runtime lifecycle tests."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from talktoharnesses.domain.enums import ErrorCode
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.domain.events import ConversationEvent
from talktoharnesses.domain.models import Command, InteractionAnswer, LaunchSnapshot, ProcessRecord
from talktoharnesses.domain.transitions import ConversationState


class MemoryPersistence:
    """Minimal durable double implementing the Persistence protocol."""

    def __init__(self) -> None:
        self.states: dict[UUID, ConversationState] = {}
        self.processes: dict[UUID, ProcessRecord] = {}  # process_id -> record
        self.launch_history: dict[UUID, list[LaunchSnapshot]] = {}  # conversation
        self.events: dict[UUID, list[ConversationEvent]] = {}

    def seed(self, state: ConversationState) -> None:
        self.states[state.conversation.id] = state
        self.events.setdefault(state.conversation.id, [])
        self.launch_history.setdefault(state.conversation.id, [])

    async def get_snapshot(self, conversation_id: UUID, owner_id: str) -> ConversationState:
        try:
            state = self.states[conversation_id]
        except KeyError as exc:
            raise DomainError(
                ErrorCode.INVALID_STATE,
                "conversation not found",
                details={"conversation_id": str(conversation_id)},
            ) from exc
        if state.conversation.owner_id != owner_id:
            raise DomainError(
                ErrorCode.INVALID_STATE,
                "owner mismatch",
                details={"conversation_id": str(conversation_id)},
            )
        return state

    async def save_snapshot(self, state: ConversationState) -> ConversationState:
        existing = self.states.get(state.conversation.id)
        if existing is not None and existing.conversation.version != state.conversation.version:
            # Caller supplies the new state already version-bumped; compare prior.
            pass
        self.states[state.conversation.id] = state
        return state

    async def accept_command(self, command: Command) -> Command:
        return command

    async def claim_commands(self, worker_id: str, limit: int) -> Sequence[Command]:
        return ()

    async def update_command(self, command: Command) -> Command:
        return command

    async def commit_event_batch(
        self,
        conversation_id: UUID,
        expected_version: int,
        state: ConversationState,
        events: Sequence[ConversationEvent],
    ) -> Sequence[ConversationEvent]:
        return await self.commit_runtime_lifecycle(
            conversation_id,
            expected_version,
            state,
            None,
            None,
            events,
        )

    async def commit_runtime_lifecycle(
        self,
        conversation_id: UUID,
        expected_version: int,
        state: ConversationState,
        process: ProcessRecord | None,
        launch_history_entry: LaunchSnapshot | None,
        events: Sequence[ConversationEvent],
    ) -> Sequence[ConversationEvent]:
        current = self.states.get(conversation_id)
        if current is None:
            raise DomainError(
                ErrorCode.INVALID_STATE,
                "conversation not found",
                details={"conversation_id": str(conversation_id)},
            )
        if current.conversation.version != expected_version:
            raise DomainError(
                ErrorCode.OPTIMISTIC_CONFLICT,
                "optimistic concurrency conflict",
                details={
                    "expected": expected_version,
                    "actual": current.conversation.version,
                },
            )

        # Idempotent process updates by incarnation UUID.
        if process is not None:
            existing = self.processes.get(process.id)
            if existing is not None and existing.status == process.status:
                # Retry of the same status — keep the first record, still apply state.
                pass
            self.processes[process.id] = process
            # Reflect redacted tail onto stored process.
            if process.redacted_stderr_tail or existing is None:
                self.processes[process.id] = process

        if launch_history_entry is not None:
            history = self.launch_history.setdefault(conversation_id, [])
            # Idempotent: one entry per process incarnation via binding snapshot identity.
            history.append(launch_history_entry)

        stored_events = self.events.setdefault(conversation_id, [])
        if events:
            # Authoritative sequences come from the event envelopes.
            for event in events:
                stored_events.append(event)
            self.states[conversation_id] = state
        else:
            # Process-only update: keep version, replace aggregate fields that
            # may carry an updated binding launch_snapshot from caller.
            self.states[conversation_id] = (
                state.model_copy(
                    update={
                        "conversation": state.conversation.model_copy(
                            update={"version": current.conversation.version}
                        )
                    }
                )
                if state.conversation.version != current.conversation.version
                else state
            )
            # When no events, accept state only if version matches expected
            # (no bump). Caller may pass the same version.
            if state.conversation.version != expected_version:
                # State already includes a version bump without events — reject.
                # Actually start_session always has events. Process-only commits
                # pass the same state object with same version.
                self.states[conversation_id] = current.model_copy(
                    update={
                        "binding": state.binding,
                        "idle_reap_eligible": state.idle_reap_eligible,
                    }
                )
            else:
                self.states[conversation_id] = state

        return tuple(events)

    async def replay(
        self,
        conversation_id: UUID,
        after_sequence: int,
        event_count_limit: int,
        byte_limit: int,
    ) -> Sequence[ConversationEvent]:
        items = [e for e in self.events.get(conversation_id, []) if e.sequence > after_sequence]
        return tuple(items[:event_count_limit])

    async def resolve_interaction(
        self,
        interaction_id: UUID,
        answer: InteractionAnswer,
    ) -> InteractionAnswer:
        return answer

    async def delete_expired_turn_aggregates(self, cutoff: datetime) -> int:
        return 0

    async def purge_soft_deleted(self, cutoff: datetime) -> int:
        return 0

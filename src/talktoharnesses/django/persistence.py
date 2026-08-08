"""Django ORM implementation of the asynchronous persistence boundary."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from typing import TypeVar
from uuid import UUID

from asgiref.sync import sync_to_async
from django.db import connection, transaction
from pydantic import BaseModel

from talktoharnesses.domain.enums import CommandStatus, ErrorCode
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.domain.events import ConversationEvent
from talktoharnesses.domain.models import Command, InteractionAnswer, LaunchSnapshot, ProcessRecord
from talktoharnesses.domain.transitions import ConversationState

from .models import (
    CommandRecord,
    ConversationAggregate,
    ConversationEventRecord,
    InteractionAnswerRecord,
    LaunchHistory,
    RuntimeProcess,
)


def _json(model: BaseModel) -> dict[str, object]:
    return model.model_dump(mode="json")


ModelT = TypeVar("ModelT", bound=BaseModel)


def _load(model: type[ModelT], value: object) -> ModelT:
    # Strict domain models intentionally accept serialized UUIDs/enums only
    # through Pydantic's JSON validation path.
    return model.model_validate_json(json.dumps(value))


def _conflict(expected: int, actual: int) -> DomainError:
    return DomainError(
        ErrorCode.OPTIMISTIC_CONFLICT,
        "optimistic concurrency conflict",
        details={"expected": expected, "actual": actual},
    )


class DjangoPersistence:
    """Production repository for SQLite and PostgreSQL.

    Synchronous transaction bodies run through asgiref's thread-sensitive
    bridge so Django connections and atomic blocks stay on one worker thread.
    """

    async def get_snapshot(self, conversation_id: UUID, owner_id: str) -> ConversationState:
        return await sync_to_async(self._get_snapshot, thread_sensitive=True)(
            conversation_id, owner_id
        )

    def _get_snapshot(self, conversation_id: UUID, owner_id: str) -> ConversationState:
        try:
            row = ConversationAggregate.objects.get(
                conversation_id=conversation_id,
                owner_id=owner_id,
            )
        except ConversationAggregate.DoesNotExist as exc:
            raise DomainError(
                ErrorCode.INVALID_STATE,
                "conversation not found",
                details={"conversation_id": str(conversation_id)},
            ) from exc
        return _load(ConversationState, row.state)

    async def save_snapshot(self, state: ConversationState) -> ConversationState:
        return await sync_to_async(self._save_snapshot, thread_sensitive=True)(state)

    @transaction.atomic
    def _save_snapshot(self, state: ConversationState) -> ConversationState:
        cid = state.conversation.id
        row = ConversationAggregate.objects.select_for_update().filter(conversation_id=cid).first()
        if row is None:
            ConversationAggregate.objects.create(**self._aggregate_values(state))
            return state
        expected = state.conversation.version - 1
        if row.version != expected:
            if row.version == state.conversation.version and row.state == _json(state):
                return state
            raise _conflict(expected, row.version)
        self._store_aggregate(row, state)
        return state

    async def accept_command(self, command: Command) -> Command:
        return await sync_to_async(self._accept_command, thread_sensitive=True)(command)

    @transaction.atomic
    def _accept_command(self, command: Command) -> Command:
        row, created = CommandRecord.objects.get_or_create(
            conversation_id=command.conversation_id,
            idempotency_key=command.idempotency_key,
            defaults=self._command_values(command),
        )
        return command if created else _load(Command, row.data)

    async def claim_commands(self, worker_id: str, limit: int) -> Sequence[Command]:
        return await sync_to_async(self._claim_commands, thread_sensitive=True)(worker_id, limit)

    @transaction.atomic
    def _claim_commands(self, worker_id: str, limit: int) -> tuple[Command, ...]:
        query = CommandRecord.objects.filter(status=CommandStatus.ACCEPTED.value).order_by(
            "command_id"
        )
        if connection.vendor == "postgresql":
            query = query.select_for_update(skip_locked=True)
        else:
            query = query.select_for_update()
        claimed: list[Command] = []
        for row in query[:limit]:
            stored = _load(Command, row.data)
            command = stored.model_copy(
                update={
                    "status": CommandStatus.CLAIMED,
                    "worker_id": worker_id,
                    "attempts": stored.attempts + 1,
                }
            )
            row.status = command.status.value
            row.worker_id = worker_id
            row.data = _json(command)
            row.save(update_fields=("status", "worker_id", "data"))
            claimed.append(command)
        return tuple(claimed)

    async def update_command(self, command: Command) -> Command:
        return await sync_to_async(self._update_command, thread_sensitive=True)(command)

    def _update_command(self, command: Command) -> Command:
        updated = CommandRecord.objects.filter(command_id=command.id).update(
            **self._command_values(command)
        )
        if not updated:
            raise DomainError(ErrorCode.INVALID_STATE, "command not found")
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
        return await sync_to_async(self._commit_runtime_lifecycle, thread_sensitive=True)(
            conversation_id,
            expected_version,
            state,
            process,
            launch_history_entry,
            tuple(events),
        )

    @transaction.atomic
    def _commit_runtime_lifecycle(
        self,
        conversation_id: UUID,
        expected_version: int,
        state: ConversationState,
        process: ProcessRecord | None,
        launch_history_entry: LaunchSnapshot | None,
        events: tuple[ConversationEvent, ...],
    ) -> tuple[ConversationEvent, ...]:
        try:
            row = ConversationAggregate.objects.select_for_update().get(
                conversation_id=conversation_id
            )
        except ConversationAggregate.DoesNotExist as exc:
            raise DomainError(ErrorCode.INVALID_STATE, "conversation not found") from exc
        if row.version != expected_version:
            raise _conflict(expected_version, row.version)
        if state.conversation.id != conversation_id:
            raise DomainError(ErrorCode.INVALID_STATE, "aggregate belongs to another conversation")

        expected_sequence = row.next_event_sequence
        for event in events:
            if event.conversation_id != conversation_id or event.sequence != expected_sequence:
                raise DomainError(ErrorCode.OPTIMISTIC_CONFLICT, "event sequence conflict")
            expected_sequence += 1
        if state.conversation.next_event_sequence != expected_sequence:
            raise DomainError(ErrorCode.OPTIMISTIC_CONFLICT, "aggregate sequence conflict")

        self._store_aggregate(row, state)
        if process is not None:
            process_row, _ = RuntimeProcess.objects.update_or_create(
                process_id=process.id,
                defaults={
                    "conversation_id": process.conversation_id,
                    "binding_id": process.binding_id,
                    "status": process.status.value,
                    "pid": process.pid,
                    "started_at": process.started_at,
                    "exited_at": process.exited_at,
                    "exit_code": process.exit_code,
                    "redacted_stderr_tail": process.redacted_stderr_tail,
                },
            )
            if launch_history_entry is not None:
                launch_json = _json(launch_history_entry)
                launch, created = LaunchHistory.objects.get_or_create(
                    process=process_row,
                    defaults={
                        "conversation_id": conversation_id,
                        "launch": launch_json,
                    },
                )
                if not created and launch.launch != launch_json:
                    raise DomainError(ErrorCode.INVALID_STATE, "launch history is immutable")
        elif launch_history_entry is not None:
            raise DomainError(ErrorCode.INVALID_STATE, "launch history requires a process")

        ConversationEventRecord.objects.bulk_create(
            [
                ConversationEventRecord(
                    event_id=event.event_id,
                    conversation_id=conversation_id,
                    sequence=event.sequence,
                    timestamp=event.timestamp,
                    type=event.type,
                    payload=_json(event),
                )
                for event in events
            ]
        )
        return events

    async def replay(
        self,
        conversation_id: UUID,
        after_sequence: int,
        event_count_limit: int,
        byte_limit: int,
    ) -> Sequence[ConversationEvent]:
        return await sync_to_async(self._replay, thread_sensitive=True)(
            conversation_id, after_sequence, event_count_limit, byte_limit
        )

    def _replay(
        self,
        conversation_id: UUID,
        after_sequence: int,
        event_count_limit: int,
        byte_limit: int,
    ) -> tuple[ConversationEvent, ...]:
        rows = ConversationEventRecord.objects.filter(
            conversation_id=conversation_id,
            sequence__gt=after_sequence,
        ).order_by("sequence")[:event_count_limit]
        events: list[ConversationEvent] = []
        size = 0
        for row in rows:
            event = _load(ConversationEvent, row.payload)
            encoded_size = len(event.model_dump_json().encode("utf-8"))
            if events and size + encoded_size > byte_limit:
                break
            if encoded_size > byte_limit:
                break
            events.append(event)
            size += encoded_size
        return tuple(events)

    async def resolve_interaction(
        self,
        interaction_id: UUID,
        answer: InteractionAnswer,
    ) -> InteractionAnswer:
        return await sync_to_async(self._resolve_interaction, thread_sensitive=True)(
            interaction_id, answer
        )

    @transaction.atomic
    def _resolve_interaction(
        self,
        interaction_id: UUID,
        answer: InteractionAnswer,
    ) -> InteractionAnswer:
        row, created = InteractionAnswerRecord.objects.get_or_create(
            interaction_id=interaction_id,
            defaults={"data": _json(answer), "submitted_at": answer.submitted_at},
        )
        return answer if created else _load(InteractionAnswer, row.data)

    async def delete_expired_turn_aggregates(self, cutoff: datetime) -> int:
        return await sync_to_async(self._delete_expired, thread_sensitive=True)(cutoff)

    def _delete_expired(self, cutoff: datetime) -> int:
        # Turn-level retention requires dedicated turn rows; aggregate JSON is
        # never deleted as a proxy because it is the canonical conversation.
        return 0

    async def purge_soft_deleted(self, cutoff: datetime) -> int:
        return await sync_to_async(self._purge_soft_deleted, thread_sensitive=True)(cutoff)

    def _purge_soft_deleted(self, cutoff: datetime) -> int:
        rows = ConversationAggregate.objects.filter(deleted_at__lt=cutoff)
        count = rows.count()
        rows.delete()
        return count

    @staticmethod
    def _aggregate_values(state: ConversationState) -> dict[str, object]:
        conversation = state.conversation
        return {
            "conversation_id": conversation.id,
            "owner_id": conversation.owner_id,
            "version": conversation.version,
            "next_event_sequence": conversation.next_event_sequence,
            "updated_at": conversation.updated_at,
            "deleted_at": conversation.deleted_at,
            "state": _json(state),
        }

    def _store_aggregate(
        self,
        row: ConversationAggregate,
        state: ConversationState,
    ) -> None:
        values = self._aggregate_values(state)
        for name, value in values.items():
            if name != "conversation_id":
                setattr(row, name, value)
        row.save(
            update_fields=(
                "owner_id",
                "version",
                "next_event_sequence",
                "updated_at",
                "deleted_at",
                "state",
            )
        )

    @staticmethod
    def _command_values(command: Command) -> dict[str, object]:
        return {
            "command_id": command.id,
            "status": command.status.value,
            "worker_id": command.worker_id,
            "lease_expires_at": command.lease_expires_at,
            "data": _json(command),
        }

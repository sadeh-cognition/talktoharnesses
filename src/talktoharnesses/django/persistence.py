"""Django ORM implementation of the asynchronous persistence boundary."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from typing import TypeVar
from uuid import UUID

from asgiref.sync import sync_to_async
from django.db import connection, models, transaction
from pydantic import BaseModel

from talktoharnesses.application.cursors import clamp_page_limit, encode_cursor
from talktoharnesses.domain.enums import CommandStatus, ErrorCode, InteractionStatus
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.domain.events import ConversationEvent
from talktoharnesses.domain.models import (
    ActivityProjection,
    Command,
    ConversationDetail,
    ConversationShell,
    ConversationSnapshot,
    HarnessCapabilities,
    HarnessInstance,
    HarnessProbeProjection,
    HarnessProjection,
    InteractionAnswer,
    InteractionProjection,
    LaunchSnapshot,
    MessageProjection,
    Page,
    PlanProjection,
    ProcessRecord,
    ToolProjection,
    TurnProjection,
)
from talktoharnesses.domain.transitions import ConversationState

from .models import (
    ActivityRecord,
    CommandRecord,
    ConversationAggregate,
    ConversationEventRecord,
    HarnessRecord,
    InteractionAnswerRecord,
    InteractionRecord,
    LaunchHistory,
    MessageRecord,
    PlanRecord,
    RuntimeProcess,
    SearchDocument,
    ToolRecord,
    TurnRecord,
)
from .projections import (
    activity_from_row,
    apply_asc_datetime_cursor,
    apply_desc_datetime_cursor,
    apply_desc_int_cursor,
    command_projection,
    harness_from_row,
    interaction_from_row,
    message_from_row,
    not_found,
    plan_from_row,
    probe_from_row,
    shell_from_row,
    tool_from_row,
    turn_from_row,
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
                deleted_at__isnull=True,
            )
        except ConversationAggregate.DoesNotExist as exc:
            raise DomainError(
                ErrorCode.INVALID_STATE,
                "conversation not found",
                details={"conversation_id": str(conversation_id)},
            ) from exc
        return _load(ConversationState, row.state)

    async def get_worker_snapshot(self, conversation_id: UUID) -> ConversationState:
        return await sync_to_async(self._get_worker_snapshot, thread_sensitive=True)(
            conversation_id
        )

    def _get_worker_snapshot(self, conversation_id: UUID) -> ConversationState:
        try:
            row = ConversationAggregate.objects.get(conversation_id=conversation_id)
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
        from talktoharnesses.django.materialize import materialize_projections

        cid = state.conversation.id
        row = ConversationAggregate.objects.select_for_update().filter(conversation_id=cid).first()
        if row is None:
            ConversationAggregate.objects.create(**self._aggregate_values(state))
            materialize_projections(state, ())
            return state
        expected = state.conversation.version - 1
        if row.version != expected:
            if row.version == state.conversation.version and row.state == _json(state):
                return state
            raise _conflict(expected, row.version)
        self._store_aggregate(row, state)
        materialize_projections(state, ())
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
        from datetime import UTC, datetime, timedelta

        now = datetime.now(UTC)
        lease_expires = now + timedelta(seconds=30)
        query = CommandRecord.objects.filter(
            models.Q(status=CommandStatus.ACCEPTED.value)
            | models.Q(
                status=CommandStatus.CLAIMED.value,
                lease_expires_at__lt=now,
            )
        ).order_by("command_id")
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
                    "lease_expires_at": lease_expires,
                }
            )
            row.status = command.status.value
            row.worker_id = worker_id
            row.lease_expires_at = lease_expires
            row.data = _json(command)
            row.save(update_fields=("status", "worker_id", "lease_expires_at", "data"))
            claimed.append(command)
        return tuple(claimed)

    async def renew_command_lease(
        self,
        command_id: UUID,
        worker_id: str,
        expires_at: datetime,
    ) -> None:
        await sync_to_async(self._renew_command_lease, thread_sensitive=True)(
            command_id, worker_id, expires_at
        )

    def _renew_command_lease(
        self,
        command_id: UUID,
        worker_id: str,
        expires_at: datetime,
    ) -> None:
        row = CommandRecord.objects.filter(command_id=command_id, worker_id=worker_id).first()
        if row is None:
            raise DomainError(ErrorCode.INVALID_STATE, "command lease not found for worker")
        command = _load(Command, row.data).model_copy(update={"lease_expires_at": expires_at})
        row.lease_expires_at = expires_at
        row.data = _json(command)
        row.save(update_fields=("lease_expires_at", "data"))

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
        return await self.commit_turn_batch(
            conversation_id,
            expected_version,
            state,
            events,
            (),
        )

    async def commit_turn_batch(
        self,
        conversation_id: UUID,
        expected_version: int,
        state: ConversationState,
        events: Sequence[ConversationEvent],
        commands: Sequence[Command] = (),
    ) -> Sequence[ConversationEvent]:
        return await sync_to_async(self._commit_turn_batch, thread_sensitive=True)(
            conversation_id,
            expected_version,
            state,
            tuple(events),
            tuple(commands),
        )

    @transaction.atomic
    def _commit_turn_batch(
        self,
        conversation_id: UUID,
        expected_version: int,
        state: ConversationState,
        events: tuple[ConversationEvent, ...],
        commands: tuple[Command, ...],
    ) -> tuple[ConversationEvent, ...]:
        committed = self._commit_runtime_lifecycle(
            conversation_id,
            expected_version,
            state,
            None,
            None,
            events,
        )
        for command in commands:
            updated = CommandRecord.objects.filter(command_id=command.id).update(
                **self._command_values(command)
            )
            if not updated:
                raise DomainError(
                    ErrorCode.INVALID_STATE,
                    "command not found",
                    details={"command_id": str(command.id)},
                )
        return committed

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
        from talktoharnesses.django.materialize import materialize_projections

        materialize_projections(state, events)
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
        binding = state.binding
        from talktoharnesses.domain.enums import InteractionStatus

        pending = any(
            i.status in {InteractionStatus.PENDING, InteractionStatus.DRAFT}
            for i in state.interactions.values()
        )
        return {
            "conversation_id": conversation.id,
            "owner_id": conversation.owner_id,
            "version": conversation.version,
            "next_event_sequence": conversation.next_event_sequence,
            "updated_at": conversation.updated_at,
            "deleted_at": conversation.deleted_at,
            "title": conversation.display_title,
            "status": conversation.status.value,
            "harness_kind": binding.kind.value if binding else None,
            "model": binding.configuration.model if binding else None,
            "mode": binding.configuration.mode if binding else None,
            "has_pending_interactions": pending,
            "pinned_at": conversation.pinned_at,
            "archived_at": conversation.archived_at,
            "snoozed_until": conversation.snoozed_until,
            "latest_activity_at": conversation.updated_at,
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
                "title",
                "status",
                "harness_kind",
                "model",
                "mode",
                "has_pending_interactions",
                "pinned_at",
                "archived_at",
                "snoozed_until",
                "latest_activity_at",
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

    # ------------------------------------------------------------------
    # Phase 5 facade projection surface
    # ------------------------------------------------------------------

    async def create_harness(self, harness: HarnessInstance) -> HarnessProjection:
        return await sync_to_async(self._create_harness, thread_sensitive=True)(harness)

    @transaction.atomic
    def _create_harness(self, harness: HarnessInstance) -> HarnessProjection:
        row = HarnessRecord.objects.create(
            harness_id=harness.id,
            owner_id=harness.owner_id,
            name=harness.name,
            kind=harness.kind.value,
            configuration=_json(harness.configuration),
            created_at=harness.created_at,
        )
        return harness_from_row(row)

    async def list_harnesses(
        self,
        owner_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> Page[HarnessProjection]:
        return await sync_to_async(self._list_harnesses, thread_sensitive=True)(
            owner_id, cursor, limit
        )

    def _list_harnesses(
        self,
        owner_id: str,
        cursor: str | None,
        limit: int,
    ) -> Page[HarnessProjection]:
        page_size = clamp_page_limit(limit)
        qs = HarnessRecord.objects.filter(owner_id=owner_id).order_by("-created_at", "-harness_id")
        qs = apply_desc_datetime_cursor(qs, cursor, "created_at", "harness_id")
        rows = list(qs[: page_size + 1])
        items = rows[:page_size]
        next_cursor = None
        if len(rows) > page_size and items:
            last = items[-1]
            next_cursor = encode_cursor(sort=last.created_at.isoformat(), id=last.harness_id)
        return Page(items=tuple(harness_from_row(r) for r in items), next_cursor=next_cursor)

    async def get_harness(self, harness_id: UUID, owner_id: str) -> HarnessProjection:
        return await sync_to_async(self._get_harness, thread_sensitive=True)(harness_id, owner_id)

    def _get_harness(self, harness_id: UUID, owner_id: str) -> HarnessProjection:
        row = HarnessRecord.objects.filter(harness_id=harness_id, owner_id=owner_id).first()
        if row is None:
            raise not_found("harness")
        return harness_from_row(row)

    async def save_harness_probe(
        self,
        harness_id: UUID,
        owner_id: str,
        capabilities: HarnessCapabilities,
        *,
        probed_at: datetime,
    ) -> HarnessProbeProjection:
        return await sync_to_async(self._save_harness_probe, thread_sensitive=True)(
            harness_id, owner_id, capabilities, probed_at
        )

    @transaction.atomic
    def _save_harness_probe(
        self,
        harness_id: UUID,
        owner_id: str,
        capabilities: HarnessCapabilities,
        probed_at: datetime,
    ) -> HarnessProbeProjection:
        row = (
            HarnessRecord.objects.select_for_update()
            .filter(harness_id=harness_id, owner_id=owner_id)
            .first()
        )
        if row is None:
            raise not_found("harness")
        row.last_probe = _json(capabilities)
        row.last_probed_at = probed_at
        row.save(update_fields=("last_probe", "last_probed_at"))
        return probe_from_row(row)

    async def get_harness_probe(
        self,
        harness_id: UUID,
        owner_id: str,
    ) -> HarnessProbeProjection:
        return await sync_to_async(self._get_harness_probe, thread_sensitive=True)(
            harness_id, owner_id
        )

    def _get_harness_probe(self, harness_id: UUID, owner_id: str) -> HarnessProbeProjection:
        row = HarnessRecord.objects.filter(harness_id=harness_id, owner_id=owner_id).first()
        if row is None:
            raise not_found("harness")
        return probe_from_row(row)

    async def list_conversations(
        self,
        owner_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
        include_archived: bool = True,
    ) -> Page[ConversationShell]:
        return await sync_to_async(self._list_conversations, thread_sensitive=True)(
            owner_id, cursor, limit, include_archived
        )

    def _list_conversations(
        self,
        owner_id: str,
        cursor: str | None,
        limit: int,
        include_archived: bool,
    ) -> Page[ConversationShell]:
        page_size = clamp_page_limit(limit)
        qs = ConversationAggregate.objects.filter(
            owner_id=owner_id,
            deleted_at__isnull=True,
        )
        if not include_archived:
            qs = qs.filter(archived_at__isnull=True)
        qs = qs.order_by("-updated_at", "-conversation_id")
        qs = apply_desc_datetime_cursor(qs, cursor, "updated_at", "conversation_id")
        rows = list(qs[: page_size + 1])
        items = rows[:page_size]
        next_cursor = None
        if len(rows) > page_size and items:
            last = items[-1]
            next_cursor = encode_cursor(sort=last.updated_at.isoformat(), id=last.conversation_id)
        return Page(items=tuple(shell_from_row(r) for r in items), next_cursor=next_cursor)

    async def search_conversations(
        self,
        owner_id: str,
        query: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> Page[ConversationShell]:
        return await sync_to_async(self._search_conversations, thread_sensitive=True)(
            owner_id, query, cursor, limit
        )

    def _search_conversations(
        self,
        owner_id: str,
        query: str,
        cursor: str | None,
        limit: int,
    ) -> Page[ConversationShell]:
        page_size = clamp_page_limit(limit)
        needle = " ".join(query.split()).casefold()
        if not needle:
            return Page(items=(), next_cursor=None)
        matching_ids = SearchDocument.objects.filter(
            owner_id=owner_id,
            normalized_text__icontains=needle,
        ).values_list("conversation_id", flat=True)
        qs = ConversationAggregate.objects.filter(
            conversation_id__in=matching_ids,
            owner_id=owner_id,
            deleted_at__isnull=True,
        ).order_by("-updated_at", "-conversation_id")
        qs = apply_desc_datetime_cursor(qs, cursor, "updated_at", "conversation_id")
        rows = list(qs[: page_size + 1])
        items = rows[:page_size]
        next_cursor = None
        if len(rows) > page_size and items:
            last = items[-1]
            next_cursor = encode_cursor(sort=last.updated_at.isoformat(), id=last.conversation_id)
        return Page(items=tuple(shell_from_row(r) for r in items), next_cursor=next_cursor)

    async def get_conversation_snapshot(
        self,
        conversation_id: UUID,
        owner_id: str,
    ) -> ConversationSnapshot:
        return await sync_to_async(self._get_conversation_snapshot, thread_sensitive=True)(
            conversation_id, owner_id
        )

    @transaction.atomic
    def _get_conversation_snapshot(
        self,
        conversation_id: UUID,
        owner_id: str,
    ) -> ConversationSnapshot:
        row = (
            ConversationAggregate.objects.select_for_update()
            .filter(
                conversation_id=conversation_id,
                owner_id=owner_id,
                deleted_at__isnull=True,
            )
            .first()
        )
        if row is None:
            raise not_found("conversation")
        state = _load(ConversationState, row.state)
        high_water = max(0, row.next_event_sequence - 1)

        user_turns = list(
            TurnRecord.objects.filter(
                conversation_id=conversation_id,
                user_message_id__isnull=False,
            ).order_by("-order_index", "-turn_id")[:20]
        )
        # Present newest-first selection in chronological order for the detail payload.
        user_turns.reverse()
        turn_projections = tuple(turn_from_row(t) for t in user_turns)
        turn_ids = [turn.turn_id for turn in user_turns]
        messages = tuple(
            message_from_row(message)
            for message in MessageRecord.objects.filter(turn_id__in=turn_ids).order_by(
                "created_at", "message_id"
            )
        )
        tools = tuple(
            tool_from_row(tool)
            for tool in ToolRecord.objects.filter(turn_id__in=turn_ids).order_by(
                "order_index", "tool_id"
            )
        )
        plans = tuple(
            plan_from_row(plan)
            for plan in PlanRecord.objects.filter(turn_id__in=turn_ids).order_by(
                "order_index", "plan_id"
            )
        )
        activity = tuple(
            activity_from_row(item)
            for item in ActivityRecord.objects.filter(parent_turn_id__in=turn_ids).order_by(
                "created_at", "activity_id"
            )
        )

        pending_rows = InteractionRecord.objects.filter(
            conversation_id=conversation_id,
            status__in=(InteractionStatus.PENDING.value, InteractionStatus.DRAFT.value),
        ).order_by("created_at", "interaction_id")
        pending = tuple(interaction_from_row(r) for r in pending_rows)

        active_command = None
        if state.active_turn is not None and state.active_turn.command_id is not None:
            cmd = state.commands.get(state.active_turn.command_id)
            if cmd is not None:
                active_command = command_projection(cmd)
        elif state.queued_turn is not None and state.queued_turn.command_id is not None:
            cmd = state.commands.get(state.queued_turn.command_id)
            if cmd is not None:
                active_command = command_projection(cmd)

        detail = ConversationDetail(
            conversation=state.conversation,
            harness_kind=state.binding.kind if state.binding else None,
            model=state.binding.configuration.model if state.binding else None,
            mode=state.binding.configuration.mode if state.binding else None,
            turns=turn_projections,
            messages=messages,
            tools=tools,
            plans=plans,
            activity=activity,
            pending_interactions=pending,
            active_command=active_command,
        )
        return ConversationSnapshot(sequence=high_water, detail=detail)

    async def get_high_water_sequence(
        self,
        conversation_id: UUID,
        owner_id: str,
        *,
        include_deleted: bool = False,
    ) -> int:
        return await sync_to_async(self._get_high_water_sequence, thread_sensitive=True)(
            conversation_id, owner_id, include_deleted
        )

    def _get_high_water_sequence(
        self,
        conversation_id: UUID,
        owner_id: str,
        include_deleted: bool,
    ) -> int:
        query = ConversationAggregate.objects.filter(
            conversation_id=conversation_id,
            owner_id=owner_id,
        )
        if not include_deleted:
            query = query.filter(deleted_at__isnull=True)
        row = query.first()
        if row is None:
            raise not_found("conversation")
        return max(0, row.next_event_sequence - 1)

    async def page_turns(
        self,
        conversation_id: UUID,
        owner_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> Page[TurnProjection]:
        return await sync_to_async(self._page_turns, thread_sensitive=True)(
            conversation_id, owner_id, cursor, limit
        )

    def _page_turns(
        self,
        conversation_id: UUID,
        owner_id: str,
        cursor: str | None,
        limit: int,
    ) -> Page[TurnProjection]:
        self._require_owned_conversation(conversation_id, owner_id)
        page_size = clamp_page_limit(limit)
        qs = TurnRecord.objects.filter(conversation_id=conversation_id).order_by(
            "-order_index", "-turn_id"
        )
        qs = apply_desc_int_cursor(qs, cursor, "order_index", "turn_id")
        rows = list(qs[: page_size + 1])
        items = rows[:page_size]
        next_cursor = None
        if len(rows) > page_size and items:
            last = items[-1]
            next_cursor = encode_cursor(sort=str(last.order_index), id=last.turn_id)
        return Page(items=tuple(turn_from_row(r) for r in items), next_cursor=next_cursor)

    async def page_messages(
        self,
        conversation_id: UUID,
        owner_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> Page[MessageProjection]:
        return await sync_to_async(self._page_messages, thread_sensitive=True)(
            conversation_id, owner_id, cursor, limit
        )

    def _page_messages(
        self,
        conversation_id: UUID,
        owner_id: str,
        cursor: str | None,
        limit: int,
    ) -> Page[MessageProjection]:
        self._require_owned_conversation(conversation_id, owner_id)
        page_size = clamp_page_limit(limit)
        qs = MessageRecord.objects.filter(conversation_id=conversation_id).order_by(
            "-created_at", "-message_id"
        )
        qs = apply_desc_datetime_cursor(qs, cursor, "created_at", "message_id")
        rows = list(qs[: page_size + 1])
        items = rows[:page_size]
        next_cursor = None
        if len(rows) > page_size and items:
            last = items[-1]
            next_cursor = encode_cursor(sort=last.created_at.isoformat(), id=last.message_id)
        return Page(items=tuple(message_from_row(r) for r in items), next_cursor=next_cursor)

    async def page_tools(
        self,
        conversation_id: UUID,
        owner_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> Page[ToolProjection]:
        return await sync_to_async(self._page_tools, thread_sensitive=True)(
            conversation_id, owner_id, cursor, limit
        )

    def _page_tools(
        self,
        conversation_id: UUID,
        owner_id: str,
        cursor: str | None,
        limit: int,
    ) -> Page[ToolProjection]:
        self._require_owned_conversation(conversation_id, owner_id)
        page_size = clamp_page_limit(limit)
        qs = ToolRecord.objects.filter(conversation_id=conversation_id).order_by(
            "-order_index", "-tool_id"
        )
        qs = apply_desc_int_cursor(qs, cursor, "order_index", "tool_id")
        rows = list(qs[: page_size + 1])
        items = rows[:page_size]
        next_cursor = None
        if len(rows) > page_size and items:
            last = items[-1]
            next_cursor = encode_cursor(sort=str(last.order_index), id=last.tool_id)
        return Page(items=tuple(tool_from_row(r) for r in items), next_cursor=next_cursor)

    async def page_plans(
        self,
        conversation_id: UUID,
        owner_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> Page[PlanProjection]:
        return await sync_to_async(self._page_plans, thread_sensitive=True)(
            conversation_id, owner_id, cursor, limit
        )

    def _page_plans(
        self,
        conversation_id: UUID,
        owner_id: str,
        cursor: str | None,
        limit: int,
    ) -> Page[PlanProjection]:
        self._require_owned_conversation(conversation_id, owner_id)
        page_size = clamp_page_limit(limit)
        qs = PlanRecord.objects.filter(conversation_id=conversation_id).order_by(
            "-order_index", "-plan_id"
        )
        qs = apply_desc_int_cursor(qs, cursor, "order_index", "plan_id")
        rows = list(qs[: page_size + 1])
        items = rows[:page_size]
        next_cursor = None
        if len(rows) > page_size and items:
            last = items[-1]
            next_cursor = encode_cursor(sort=str(last.order_index), id=last.plan_id)
        return Page(items=tuple(plan_from_row(r) for r in items), next_cursor=next_cursor)

    async def page_activity(
        self,
        conversation_id: UUID,
        owner_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> Page[ActivityProjection]:
        return await sync_to_async(self._page_activity, thread_sensitive=True)(
            conversation_id, owner_id, cursor, limit
        )

    def _page_activity(
        self,
        conversation_id: UUID,
        owner_id: str,
        cursor: str | None,
        limit: int,
    ) -> Page[ActivityProjection]:
        self._require_owned_conversation(conversation_id, owner_id)
        page_size = clamp_page_limit(limit)
        qs = ActivityRecord.objects.filter(conversation_id=conversation_id).order_by(
            "-created_at", "-activity_id"
        )
        qs = apply_desc_datetime_cursor(qs, cursor, "created_at", "activity_id")
        rows = list(qs[: page_size + 1])
        items = rows[:page_size]
        next_cursor = None
        if len(rows) > page_size and items:
            last = items[-1]
            next_cursor = encode_cursor(sort=last.created_at.isoformat(), id=last.activity_id)
        return Page(items=tuple(activity_from_row(r) for r in items), next_cursor=next_cursor)

    async def page_pending_interactions(
        self,
        conversation_id: UUID,
        owner_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> Page[InteractionProjection]:
        return await sync_to_async(self._page_pending_interactions, thread_sensitive=True)(
            conversation_id, owner_id, cursor, limit
        )

    def _page_pending_interactions(
        self,
        conversation_id: UUID,
        owner_id: str,
        cursor: str | None,
        limit: int,
    ) -> Page[InteractionProjection]:
        self._require_owned_conversation(conversation_id, owner_id)
        page_size = clamp_page_limit(limit)
        qs = InteractionRecord.objects.filter(
            conversation_id=conversation_id,
            status__in=(InteractionStatus.PENDING.value, InteractionStatus.DRAFT.value),
        ).order_by("created_at", "interaction_id")
        qs = apply_asc_datetime_cursor(qs, cursor, "created_at", "interaction_id")
        rows = list(qs[: page_size + 1])
        items = rows[:page_size]
        next_cursor = None
        if len(rows) > page_size and items:
            last = items[-1]
            next_cursor = encode_cursor(sort=last.created_at.isoformat(), id=last.interaction_id)
        return Page(items=tuple(interaction_from_row(r) for r in items), next_cursor=next_cursor)

    async def commit_facade_mutation(
        self,
        conversation_id: UUID,
        owner_id: str,
        expected_version: int,
        state: ConversationState,
        events: Sequence[ConversationEvent],
        commands: Sequence[Command] = (),
        interaction_answers: Sequence[InteractionAnswer] = (),
    ) -> Sequence[ConversationEvent]:
        return await sync_to_async(self._commit_facade_mutation, thread_sensitive=True)(
            conversation_id,
            owner_id,
            expected_version,
            state,
            tuple(events),
            tuple(commands),
            tuple(interaction_answers),
        )

    @transaction.atomic
    def _commit_facade_mutation(
        self,
        conversation_id: UUID,
        owner_id: str,
        expected_version: int,
        state: ConversationState,
        events: tuple[ConversationEvent, ...],
        commands: tuple[Command, ...],
        interaction_answers: tuple[InteractionAnswer, ...],
    ) -> tuple[ConversationEvent, ...]:
        row = (
            ConversationAggregate.objects.select_for_update()
            .filter(
                conversation_id=conversation_id,
                owner_id=owner_id,
                deleted_at__isnull=True,
            )
            .first()
        )
        # Soft-deleted or cross-owner rows look like missing resources.
        if row is None:
            # Allow soft-delete mutation when the aggregate exists but is not yet deleted.
            row = (
                ConversationAggregate.objects.select_for_update()
                .filter(conversation_id=conversation_id, owner_id=owner_id)
                .first()
            )
            if row is None:
                raise not_found("conversation")
            if row.deleted_at is not None and state.conversation.deleted_at is None:
                raise not_found("conversation")
        if row.version != expected_version:
            raise _conflict(expected_version, row.version)
        if state.conversation.id != conversation_id:
            raise DomainError(ErrorCode.INVALID_STATE, "aggregate belongs to another conversation")
        if state.conversation.owner_id != owner_id:
            raise not_found("conversation")

        expected_sequence = row.next_event_sequence
        for event in events:
            if event.conversation_id != conversation_id or event.sequence != expected_sequence:
                raise DomainError(ErrorCode.OPTIMISTIC_CONFLICT, "event sequence conflict")
            expected_sequence += 1
        if state.conversation.next_event_sequence != expected_sequence:
            raise DomainError(ErrorCode.OPTIMISTIC_CONFLICT, "aggregate sequence conflict")

        self._store_aggregate(row, state)
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
        for command in commands:
            CommandRecord.objects.update_or_create(
                command_id=command.id,
                defaults={
                    "conversation_id": conversation_id,
                    "idempotency_key": command.idempotency_key,
                    **self._command_values(command),
                },
            )
        for answer in interaction_answers:
            InteractionAnswerRecord.objects.get_or_create(
                interaction_id=answer.interaction_id,
                defaults={"data": _json(answer), "submitted_at": answer.submitted_at},
            )
        from talktoharnesses.django.materialize import materialize_projections

        materialize_projections(state, events)
        return events

    def _require_owned_conversation(self, conversation_id: UUID, owner_id: str) -> None:
        exists = ConversationAggregate.objects.filter(
            conversation_id=conversation_id,
            owner_id=owner_id,
            deleted_at__isnull=True,
        ).exists()
        if not exists:
            raise not_found("conversation")

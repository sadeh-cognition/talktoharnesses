"""Django ORM implementation of the asynchronous persistence boundary."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import TypeVar, cast
from uuid import UUID, uuid4

from asgiref.sync import sync_to_async
from django.db import connection, models, transaction
from django.utils.dateparse import parse_datetime
from pydantic import BaseModel

from talktoharnesses.application.cursors import clamp_page_limit, encode_cursor
from talktoharnesses.application.handoff import (
    HandoffDocument,
    HandoffMessage,
    HandoffTool,
    handoff_sort_key,
)
from talktoharnesses.application.persistence import (
    ClaimedCommand,
    ConversationOwnership,
    LostLease,
    PruneResult,
    RecoveryAttempt,
    SwitchPreparation,
)
from talktoharnesses.application.search_documents import normalize_search_terms
from talktoharnesses.domain.approval_matching import (
    InteractionMatchContext,
    rule_matches_request,
    select_matching_rule,
)
from talktoharnesses.domain.enums import (
    ActivityStatus,
    ApprovalDecision,
    ApprovalRuleDecision,
    CommandStatus,
    ConversationStatus,
    ErrorCode,
    HarnessKind,
    InteractionKind,
    InteractionStatus,
    MessageRole,
    ObservedDeliveryPhase,
    ProcessStatus,
    RecoveryAction,
    RecoveryReasonCode,
    RecoveryResultCode,
    RecoveryTrigger,
    ToolOutcome,
    TurnStatus,
)
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.domain.events import ConversationEvent, event_turn_id
from talktoharnesses.domain.models import (
    ActivityProjection,
    ApprovalAction,
    ApprovalMatcher,
    ApprovalRequestPayload,
    ApprovalRule,
    ApprovalRuleProjection,
    ApprovalRuleScope,
    CanonicalToolResult,
    Command,
    ConversationDetail,
    ConversationShell,
    ConversationSnapshot,
    HarnessCapabilities,
    HarnessInstance,
    HarnessProbeProjection,
    HarnessProjection,
    InteractionAnswer,
    InteractionAuditProjection,
    InteractionProjection,
    InteractionResolutionResult,
    LaunchSnapshot,
    MessageProjection,
    Page,
    PlanProjection,
    ProcessRecord,
    ToolProjection,
    TurnProjection,
)
from talktoharnesses.domain.transitions import (
    ConversationState,
    interrupt_turn,
    mark_requires_recreation,
    rotate_session,
    submit_interaction_answer,
)

from .models import (
    ActivityRecord,
    ApprovalRuleRecord,
    CommandRecord,
    ConversationAggregate,
    ConversationEventRecord,
    HarnessRecord,
    InteractionAnswerRecord,
    InteractionAuditRecord,
    InteractionRecord,
    LaunchHistory,
    MessageRecord,
    PlanRecord,
    RecoveryAttemptRecord,
    RuntimeProcess,
    ToolRecord,
    TurnRecord,
    WorkerLeaseRecord,
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

_SQLITE_SUPERVISOR_SLOT = "sqlite-supervisor"
_ACTIVE_RECOVERY_STATUSES = (
    ConversationStatus.RUNNING.value,
    ConversationStatus.WAITING.value,
    ConversationStatus.BACKGROUND_ACTIVE.value,
)
_RENEWABLE_COMMAND_STATUSES = (
    CommandStatus.CLAIMED.value,
    CommandStatus.DELIVERY_STARTED.value,
    CommandStatus.DELIVERED.value,
)


def _json(model: BaseModel) -> dict[str, object]:
    return model.model_dump(mode="json")


_TERMINAL_TURN_STATUSES = (
    TurnStatus.COMPLETED.value,
    TurnStatus.INTERRUPTED.value,
    TurnStatus.FAILED.value,
    TurnStatus.OUTCOME_UNKNOWN.value,
)


ModelT = TypeVar("ModelT", bound=BaseModel)


def _load(model: type[ModelT], value: object) -> ModelT:
    # Strict domain models intentionally accept serialized UUIDs/enums only
    # through Pydantic's JSON validation path.
    return model.model_validate_json(json.dumps(value))


def _insert_events(
    conversation_id: UUID,
    events: Sequence[ConversationEvent],
    state: ConversationState,
) -> None:
    """Insert committed event rows with their turn link for turn-owned deletion."""
    interaction_turn_ids = {
        interaction_id: interaction.turn_id
        for interaction_id, interaction in state.interactions.items()
    }
    ConversationEventRecord.objects.bulk_create(
        [
            ConversationEventRecord(
                event_id=event.event_id,
                conversation_id=conversation_id,
                sequence=event.sequence,
                timestamp=event.timestamp,
                type=event.type,
                payload=_json(event),
                turn_id=event_turn_id(event, interaction_turn_ids),
            )
            for event in events
        ]
    )


def _full_text_matches(terms: tuple[str, ...]) -> list[object]:
    """Return conversation IDs whose search document contains every term.

    Both backends query the private index built by ``0006_phase8_fts`` from the
    same normalized term stream; owner, soft-delete, cursor, and limit
    predicates stay on ``ConversationAggregate``. The PostgreSQL column and
    SQLite virtual table are private to this module so a SQLite install never
    imports ``django.contrib.postgres`` or Psycopg.
    """
    if connection.vendor == "postgresql":
        sql = (
            "SELECT conversation_id FROM talktoharnesses_search_document "
            "WHERE search_vector @@ plainto_tsquery('simple', %s)"
        )
        parameter = " ".join(terms)
    else:
        sql = (
            "SELECT conversation_id FROM talktoharnesses_search_document_fts "
            "WHERE talktoharnesses_search_document_fts MATCH %s"
        )
        parameter = " AND ".join(f'"{term}"' for term in terms)
    with connection.cursor() as cursor:
        cursor.execute(sql, [parameter])
        return [row[0] for row in cursor.fetchall()]


def _conflict(expected: int, actual: int) -> DomainError:
    return DomainError(
        ErrorCode.OPTIMISTIC_CONFLICT,
        "optimistic concurrency conflict",
        details={"expected": expected, "actual": actual},
    )


def _db_now() -> datetime:
    """Read the database clock used by all lease comparisons and expiries."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT statement_timestamp()"
            if connection.vendor == "postgresql"
            else "SELECT CURRENT_TIMESTAMP"
        )
        value = cursor.fetchone()[0]
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    parsed = parse_datetime(str(value))
    if parsed is None:
        raise RuntimeError("database returned an invalid timestamp")
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _stale_owner(*, conversation_id: UUID | None = None) -> DomainError:
    details: dict[str, object] = {}
    if conversation_id is not None:
        details["conversation_id"] = str(conversation_id)
    return DomainError(ErrorCode.STALE_OWNER, "stale conversation owner", details=details)


def mark_processes_orphaned(conversation_id: UUID, *, now: datetime) -> None:
    """Mark starting/running process incarnations orphaned on takeover."""
    RuntimeProcess.objects.filter(
        conversation_id=conversation_id,
        status__in=(ProcessStatus.STARTING.value, ProcessStatus.RUNNING.value),
    ).update(status=ProcessStatus.ORPHANED.value, orphaned_at=now)


def _rule_projection(rule: ApprovalRule) -> ApprovalRuleProjection:
    return ApprovalRuleProjection(
        id=rule.id,
        principal_id=rule.principal_id,
        decision=rule.decision,
        scope=rule.scope,
        matcher=rule.matcher,
        created_at=rule.created_at,
        updated_at=rule.updated_at,
    )


def _rule_from_row(row: ApprovalRuleRecord) -> ApprovalRuleProjection:
    return _rule_projection(_rule_domain_from_row(row))


def _rule_domain_from_row(row: ApprovalRuleRecord) -> ApprovalRule:
    from pydantic import TypeAdapter

    scope = cast(
        ApprovalRuleScope,
        TypeAdapter(ApprovalRuleScope).validate_json(json.dumps(row.scope)),
    )
    matcher = cast(
        ApprovalMatcher,
        TypeAdapter(ApprovalMatcher).validate_json(json.dumps(row.matcher)),
    )
    return ApprovalRule(
        id=row.rule_id,
        principal_id=row.principal_id,
        decision=ApprovalRuleDecision(row.decision),
        scope=scope,
        matcher=matcher,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _audit_from_row(row: InteractionAuditRecord) -> InteractionAuditProjection:
    from pydantic import TypeAdapter

    scope = (
        cast(
            ApprovalRuleScope,
            TypeAdapter(ApprovalRuleScope).validate_json(json.dumps(row.rule_scope)),
        )
        if row.rule_scope is not None
        else None
    )
    matcher = (
        cast(
            ApprovalMatcher,
            TypeAdapter(ApprovalMatcher).validate_json(json.dumps(row.rule_matcher)),
        )
        if row.rule_matcher is not None
        else None
    )
    action = (
        cast(
            ApprovalAction,
            TypeAdapter(ApprovalAction).validate_json(json.dumps(row.request_action)),
        )
        if row.request_action is not None
        else None
    )
    return InteractionAuditProjection(
        id=row.audit_id,
        principal_id=row.principal_id,
        interaction_id=row.interaction_id,
        conversation_id=row.conversation_id,
        turn_id=row.turn_id,
        kind=InteractionKind(row.kind),
        decision=ApprovalDecision(row.decision) if row.decision else None,
        answers=row.answers,
        automatic=row.automatic,
        created_at=row.created_at,
        provider_kind=HarnessKind(row.provider_kind) if row.provider_kind else None,
        provider_request_ids={str(k): str(v) for k, v in (row.provider_request_ids or {}).items()},
        deciding_rule_id=row.deciding_rule_id_copy,
        rule_decision=ApprovalRuleDecision(row.rule_decision) if row.rule_decision else None,
        rule_scope=scope,
        rule_matcher=matcher,
        request_action=action,
    )


def _request_action(interaction: object) -> ApprovalAction | None:
    from talktoharnesses.domain.models import ApprovalRequestPayload, PendingInteraction

    if not isinstance(interaction, PendingInteraction):
        return None
    if isinstance(interaction.request, ApprovalRequestPayload):
        return interaction.request.action
    return None


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

    async def claim_commands(
        self,
        worker_id: str,
        limit: int,
        *,
        lease_duration: float,
    ) -> Sequence[ClaimedCommand]:
        return await sync_to_async(self._claim_commands, thread_sensitive=True)(
            worker_id, limit, lease_duration
        )

    @transaction.atomic
    def _claim_commands(
        self,
        worker_id: str,
        limit: int,
        lease_duration: float,
    ) -> tuple[ClaimedCommand, ...]:
        now = _db_now()
        lease_expires = now + timedelta(seconds=lease_duration)
        query = CommandRecord.objects.filter(
            models.Q(status=CommandStatus.ACCEPTED.value)
            | models.Q(
                status=CommandStatus.CLAIMED.value,
                lease_expires_at__lt=now,
                data__delivery_started_at=None,
            )
        ).order_by("command_id")
        if connection.vendor == "postgresql":
            query = query.select_for_update(skip_locked=True)
        else:
            query = query.select_for_update()
        claimed: list[ClaimedCommand] = []
        for row in query[:limit]:
            fence = self._acquire_conversation_owner(
                row.conversation_id,
                worker_id,
                lease_duration=lease_duration,
                now=now,
            )
            if fence is None:
                continue
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
            claimed.append(ClaimedCommand(command=command, fence=fence))
        return tuple(claimed)

    async def renew_command_lease(
        self,
        command_id: UUID,
        worker_id: str,
        *,
        lease_duration: float,
        fence: int | None = None,
    ) -> None:
        await sync_to_async(self._renew_command_lease, thread_sensitive=True)(
            command_id, worker_id, lease_duration, fence
        )

    @transaction.atomic
    def _renew_command_lease(
        self,
        command_id: UUID,
        worker_id: str,
        lease_duration: float,
        fence: int | None,
    ) -> None:
        expires_at = _db_now() + timedelta(seconds=lease_duration)
        row = CommandRecord.objects.filter(
            command_id=command_id,
            worker_id=worker_id,
            status__in=_RENEWABLE_COMMAND_STATUSES,
        ).first()
        if row is None:
            raise DomainError(ErrorCode.INVALID_STATE, "command lease not found for worker")
        if fence is not None:
            self._require_conversation_owner(row.conversation_id, worker_id, fence)
        command = _load(Command, row.data).model_copy(update={"lease_expires_at": expires_at})
        row.lease_expires_at = expires_at
        row.data = _json(command)
        row.save(update_fields=("lease_expires_at", "data"))

    async def update_command(
        self,
        command: Command,
        *,
        worker_id: str | None = None,
        fence: int | None = None,
    ) -> Command:
        return await sync_to_async(self._update_command, thread_sensitive=True)(
            command, worker_id, fence
        )

    def _update_command(
        self,
        command: Command,
        worker_id: str | None,
        fence: int | None,
    ) -> Command:
        if worker_id is not None or fence is not None:
            self._require_conversation_owner(command.conversation_id, worker_id, fence)
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
        *,
        worker_id: str | None = None,
        fence: int | None = None,
    ) -> Sequence[ConversationEvent]:
        return await self.commit_turn_batch(
            conversation_id,
            expected_version,
            state,
            events,
            (),
            worker_id=worker_id,
            fence=fence,
        )

    async def commit_turn_batch(
        self,
        conversation_id: UUID,
        expected_version: int,
        state: ConversationState,
        events: Sequence[ConversationEvent],
        commands: Sequence[Command] = (),
        *,
        worker_id: str | None = None,
        fence: int | None = None,
    ) -> Sequence[ConversationEvent]:
        return await sync_to_async(self._commit_turn_batch, thread_sensitive=True)(
            conversation_id,
            expected_version,
            state,
            tuple(events),
            tuple(commands),
            worker_id,
            fence,
        )

    @transaction.atomic
    def _commit_turn_batch(
        self,
        conversation_id: UUID,
        expected_version: int,
        state: ConversationState,
        events: tuple[ConversationEvent, ...],
        commands: tuple[Command, ...],
        worker_id: str | None = None,
        fence: int | None = None,
    ) -> tuple[ConversationEvent, ...]:
        committed = self._commit_runtime_lifecycle(
            conversation_id,
            expected_version,
            state,
            None,
            None,
            events,
            worker_id=worker_id,
            fence=fence,
        )
        for command in commands:
            self._settle_command(command)
        return committed

    async def commit_runtime_lifecycle(
        self,
        conversation_id: UUID,
        expected_version: int,
        state: ConversationState,
        process: ProcessRecord | None,
        launch_history_entry: LaunchSnapshot | None,
        events: Sequence[ConversationEvent],
        *,
        worker_id: str | None = None,
        fence: int | None = None,
    ) -> Sequence[ConversationEvent]:
        return await sync_to_async(self._commit_runtime_lifecycle, thread_sensitive=True)(
            conversation_id,
            expected_version,
            state,
            process,
            launch_history_entry,
            tuple(events),
            worker_id,
            fence,
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
        worker_id: str | None = None,
        fence: int | None = None,
    ) -> tuple[ConversationEvent, ...]:
        try:
            row = ConversationAggregate.objects.select_for_update().get(
                conversation_id=conversation_id
            )
        except ConversationAggregate.DoesNotExist as exc:
            raise DomainError(ErrorCode.INVALID_STATE, "conversation not found") from exc
        self._require_owner_on_row(row, worker_id, fence)
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
                    "orphaned_at": process.orphaned_at,
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

        _insert_events(conversation_id, events, state)
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

    async def commit_interaction_request(
        self,
        conversation_id: UUID,
        expected_version: int,
        state: ConversationState,
        events: Sequence[ConversationEvent],
        *,
        interaction_id: UUID,
        provider_correlation: dict[str, str] | None = None,
        request_event_sequence: int,
        worker_id: str | None = None,
        fence: int | None = None,
    ) -> Sequence[ConversationEvent]:
        return await sync_to_async(self._commit_interaction_request, thread_sensitive=True)(
            conversation_id,
            expected_version,
            state,
            tuple(events),
            interaction_id,
            provider_correlation,
            request_event_sequence,
            worker_id,
            fence,
        )

    @transaction.atomic
    def _commit_interaction_request(
        self,
        conversation_id: UUID,
        expected_version: int,
        state: ConversationState,
        events: tuple[ConversationEvent, ...],
        interaction_id: UUID,
        provider_correlation: dict[str, str] | None,
        request_event_sequence: int,
        worker_id: str | None = None,
        fence: int | None = None,
    ) -> tuple[ConversationEvent, ...]:
        committed = self._commit_turn_batch_sync(
            conversation_id,
            expected_version,
            state,
            events,
            (),
            worker_id=worker_id,
            fence=fence,
        )
        InteractionRecord.objects.filter(interaction_id=interaction_id).update(
            provider_correlation=provider_correlation or {},
            request_event_sequence=request_event_sequence,
        )
        return committed

    async def commit_interaction_resolution(
        self,
        conversation_id: UUID,
        owner_id: str,
        expected_version: int,
        state: ConversationState,
        events: Sequence[ConversationEvent],
        answer: InteractionAnswer,
        *,
        automatic: bool = False,
        create_rule: ApprovalRule | None = None,
        deciding_rule: ApprovalRule | None = None,
        provider_kind: str | None = None,
        provider_request_ids: dict[str, str] | None = None,
        resolution_event_sequence: int,
        mark_policy_evaluated: bool = False,
        interaction_id: UUID | None = None,
        suppress_answer_command: bool = False,
        worker_id: str | None = None,
        fence: int | None = None,
    ) -> InteractionResolutionResult:
        return await sync_to_async(self._commit_interaction_resolution, thread_sensitive=True)(
            conversation_id,
            owner_id,
            expected_version,
            state,
            tuple(events),
            answer,
            automatic,
            create_rule,
            deciding_rule,
            provider_kind,
            provider_request_ids,
            resolution_event_sequence,
            mark_policy_evaluated,
            interaction_id,
            suppress_answer_command,
            worker_id,
            fence,
        )

    @transaction.atomic
    def _commit_interaction_resolution(
        self,
        conversation_id: UUID,
        owner_id: str,
        expected_version: int,
        state: ConversationState,
        events: tuple[ConversationEvent, ...],
        answer: InteractionAnswer,
        automatic: bool,
        create_rule: ApprovalRule | None,
        deciding_rule: ApprovalRule | None,
        provider_kind: str | None,
        provider_request_ids: dict[str, str] | None,
        resolution_event_sequence: int,
        mark_policy_evaluated: bool,
        interaction_id: UUID | None,
        suppress_answer_command: bool,
        worker_id: str | None,
        fence: int | None,
    ) -> InteractionResolutionResult:
        iid = interaction_id or answer.interaction_id
        existing = InteractionAnswerRecord.objects.filter(interaction_id=iid).first()
        if existing is not None:
            stored = _load(InteractionAnswer, existing.data)
            return InteractionResolutionResult(
                answer=stored,
                command=None,
                was_first_write=False,
                audit=None,
            )

        row = (
            ConversationAggregate.objects.select_for_update()
            .filter(conversation_id=conversation_id, owner_id=owner_id)
            .first()
        )
        if row is None:
            raise not_found("conversation")
        self._require_owner_on_row(row, worker_id, fence)
        existing = InteractionAnswerRecord.objects.filter(interaction_id=iid).first()
        if existing is not None:
            stored = _load(InteractionAnswer, existing.data)
            return InteractionResolutionResult(
                answer=stored,
                command=None,
                was_first_write=False,
                audit=None,
            )

        current_state = _load(ConversationState, row.state)
        interaction_row = (
            InteractionRecord.objects.select_for_update().filter(interaction_id=iid).first()
        )
        if interaction_row is None:
            raise not_found("interaction")

        if automatic:
            interaction = current_state.interactions.get(iid)
            action = _request_action(interaction)
            rules = [
                _rule_domain_from_row(rule_row)
                for rule_row in ApprovalRuleRecord.objects.select_for_update().filter(
                    principal_id=owner_id
                )
            ]
            working_directory = (
                current_state.binding.launch_snapshot.working_directory
                if current_state.binding and current_state.binding.launch_snapshot
                else None
            )
            match = select_matching_rule(
                rules,
                action=action,
                ctx=InteractionMatchContext(
                    principal_id=owner_id,
                    conversation_id=conversation_id,
                    owner_id=owner_id,
                    binding=current_state.binding,
                    working_directory=working_directory,
                ),
            )
            immediate = None
            if match.decision is ApprovalRuleDecision.ALLOW:
                immediate = ApprovalDecision.ALLOW_ONCE
            elif match.decision is ApprovalRuleDecision.DENY:
                immediate = ApprovalDecision.DENY
            available = (
                interaction.request.available_decisions
                if interaction is not None
                and isinstance(interaction.request, ApprovalRequestPayload)
                else ()
            )
            if immediate is None or immediate not in available or match.rule is None:
                interaction_row.policy_evaluated_at = current_state.conversation.updated_at
                interaction_row.save(update_fields=("policy_evaluated_at",))
                return InteractionResolutionResult(
                    answer=answer,
                    command=None,
                    was_first_write=False,
                    audit=None,
                )
            automatic_result = submit_interaction_answer(
                current_state,
                InteractionAnswer(interaction_id=iid, decision=immediate),
                now=answer.submitted_at or state.conversation.updated_at,
                automatic=True,
            )
            state = automatic_result.state
            events = automatic_result.events
            answer = state.answers[iid]
            resolution_event_sequence = events[-1].sequence
            deciding_rule = match.rule
            expected_version = current_state.conversation.version

        if row.version != expected_version:
            raise _conflict(expected_version, row.version)

        live_rule: ApprovalRule | None = deciding_rule
        if create_rule is not None:
            ApprovalRuleRecord.objects.create(
                rule_id=create_rule.id,
                principal_id=create_rule.principal_id,
                decision=create_rule.decision.value,
                scope_kind=create_rule.scope.kind,
                scope=_json(create_rule.scope),
                matcher_kind=create_rule.matcher.kind,
                matcher=_json(create_rule.matcher),
                created_at=create_rule.created_at,
                updated_at=create_rule.updated_at,
            )
            live_rule = create_rule
        elif deciding_rule is not None:
            rule_row = (
                ApprovalRuleRecord.objects.select_for_update()
                .filter(rule_id=deciding_rule.id, principal_id=owner_id)
                .first()
            )
            if rule_row is None:
                raise not_found("approval rule")
            live_rule = _rule_domain_from_row(rule_row)

        locked_interaction = state.interactions.get(iid)
        if create_rule is not None:
            ctx = InteractionMatchContext(
                principal_id=owner_id,
                conversation_id=conversation_id,
                owner_id=owner_id,
                binding=state.binding,
                working_directory=(
                    state.binding.launch_snapshot.working_directory
                    if state.binding and state.binding.launch_snapshot
                    else None
                ),
            )
            if (
                create_rule.decision is not ApprovalRuleDecision.ALLOW
                or answer.decision is not ApprovalDecision.ALLOW_ONCE
                or not rule_matches_request(
                    create_rule,
                    action=_request_action(locked_interaction),
                    ctx=ctx,
                )
            ):
                raise DomainError(
                    ErrorCode.INVALID_STATE,
                    "create-and-allow rule does not match the locked interaction",
                )

        if events:
            self._commit_turn_batch_sync(
                conversation_id,
                expected_version,
                state,
                events,
                (),
                worker_id=worker_id,
                fence=fence,
            )
        elif mark_policy_evaluated:
            InteractionRecord.objects.filter(interaction_id=iid).update(
                policy_evaluated_at=state.conversation.updated_at
            )
            return InteractionResolutionResult(
                answer=answer,
                command=None,
                was_first_write=False,
                audit=None,
            )

        audit = self._persist_interaction_answer(
            conversation_id=conversation_id,
            owner_id=owner_id,
            state=state,
            interaction_id=iid,
            answer=answer,
            interaction_row=interaction_row,
            automatic=automatic,
            live_rule=live_rule,
            provider_kind=provider_kind,
            provider_request_ids=provider_request_ids,
            resolution_event_sequence=resolution_event_sequence,
            suppress_answer_command=suppress_answer_command,
        )
        if mark_policy_evaluated or automatic:
            InteractionRecord.objects.filter(interaction_id=iid).update(
                policy_evaluated_at=answer.submitted_at or state.conversation.updated_at
            )
        return InteractionResolutionResult(
            answer=answer,
            command=None,
            was_first_write=True,
            audit=audit,
        )

    def _persist_interaction_answer(
        self,
        *,
        conversation_id: UUID,
        owner_id: str,
        state: ConversationState,
        interaction_id: UUID,
        answer: InteractionAnswer,
        interaction_row: InteractionRecord,
        automatic: bool,
        live_rule: ApprovalRule | None,
        provider_kind: str | None,
        provider_request_ids: dict[str, str] | None,
        resolution_event_sequence: int,
        suppress_answer_command: bool,
    ) -> InteractionAuditProjection:
        interaction = state.interactions.get(interaction_id)
        correlation = {
            str(key): str(value)
            for key, value in (interaction_row.provider_correlation or {}).items()
        }
        if provider_request_ids:
            correlation.update(provider_request_ids)
        resolved_provider_kind = provider_kind
        if resolved_provider_kind is None and state.binding is not None:
            resolved_provider_kind = state.binding.kind.value
        audit = InteractionAuditProjection(
            id=uuid4(),
            principal_id=owner_id,
            interaction_id=interaction_id,
            conversation_id=conversation_id,
            turn_id=interaction.turn_id if interaction else uuid4(),
            kind=interaction.kind if interaction else InteractionKind.APPROVAL,
            decision=answer.decision,
            answers=answer.answers,
            automatic=automatic,
            created_at=answer.submitted_at or state.conversation.updated_at,
            provider_kind=(HarnessKind(resolved_provider_kind) if resolved_provider_kind else None),
            provider_request_ids=correlation,
            deciding_rule_id=live_rule.id if live_rule else None,
            rule_decision=live_rule.decision if live_rule else None,
            rule_scope=live_rule.scope if live_rule else None,
            rule_matcher=live_rule.matcher if live_rule else None,
            request_action=_request_action(interaction),
        )
        InteractionAnswerRecord.objects.create(
            interaction_id=interaction_id,
            conversation_id=conversation_id,
            data=_json(answer),
            submitted_at=answer.submitted_at,
            resolution_event_sequence=resolution_event_sequence,
            released_at=None,
            answer_command_suppressed=suppress_answer_command,
        )
        InteractionAuditRecord.objects.create(
            audit_id=audit.id,
            principal_id=audit.principal_id,
            interaction_id=audit.interaction_id,
            conversation_id=audit.conversation_id,
            turn_id=audit.turn_id,
            kind=audit.kind.value,
            decision=audit.decision.value if audit.decision else None,
            answers=audit.answers,
            automatic=audit.automatic,
            created_at=audit.created_at,
            provider_kind=audit.provider_kind.value if audit.provider_kind else None,
            provider_request_ids=audit.provider_request_ids,
            deciding_rule_id=live_rule.id if live_rule else None,
            deciding_rule_id_copy=live_rule.id if live_rule else None,
            rule_decision=live_rule.decision.value if live_rule else None,
            rule_scope=_json(live_rule.scope) if live_rule else None,
            rule_matcher=_json(live_rule.matcher) if live_rule else None,
            request_action=_json(audit.request_action) if audit.request_action else None,
        )
        return audit

    async def release_interaction_answer(
        self,
        conversation_id: UUID,
        owner_id: str,
        interaction_id: UUID,
        command: Command,
        *,
        expected_version: int,
        state: ConversationState,
        worker_id: str | None = None,
        fence: int | None = None,
    ) -> Command:
        return await sync_to_async(self._release_interaction_answer, thread_sensitive=True)(
            conversation_id,
            owner_id,
            interaction_id,
            command,
            expected_version,
            state,
            worker_id,
            fence,
        )

    @transaction.atomic
    def _release_interaction_answer(
        self,
        conversation_id: UUID,
        owner_id: str,
        interaction_id: UUID,
        command: Command,
        expected_version: int,
        state: ConversationState,
        worker_id: str | None,
        fence: int | None,
    ) -> Command:
        answer_row = (
            InteractionAnswerRecord.objects.select_for_update()
            .filter(interaction_id=interaction_id)
            .first()
        )
        if answer_row is None:
            raise DomainError(ErrorCode.INVALID_STATE, "interaction answer missing for release")
        if answer_row.command is not None:
            return _load(Command, answer_row.command.data)
        row = (
            ConversationAggregate.objects.select_for_update()
            .filter(conversation_id=conversation_id, owner_id=owner_id)
            .first()
        )
        if row is None:
            raise not_found("conversation")
        self._require_owner_on_row(row, worker_id, fence)
        current_state = _load(ConversationState, row.state)
        command_row, _ = CommandRecord.objects.update_or_create(
            command_id=command.id,
            defaults={
                "conversation_id": conversation_id,
                "idempotency_key": command.idempotency_key,
                **self._command_values(command),
            },
        )
        answer_row.command = command_row
        answer_row.released_at = command.created_at
        answer_row.save(update_fields=["command", "released_at"])
        commands = dict(current_state.commands)
        commands[command.id] = command
        self._store_aggregate(row, current_state.model_copy(update={"commands": commands}))
        return command

    async def get_interaction_resolution_event(
        self,
        conversation_id: UUID,
        interaction_id: UUID,
    ) -> ConversationEvent:
        return await sync_to_async(self._get_interaction_resolution_event, thread_sensitive=True)(
            conversation_id,
            interaction_id,
        )

    def _get_interaction_resolution_event(
        self,
        conversation_id: UUID,
        interaction_id: UUID,
    ) -> ConversationEvent:
        answer = InteractionAnswerRecord.objects.filter(
            interaction_id=interaction_id,
            conversation_id=conversation_id,
        ).first()
        if answer is None or answer.resolution_event_sequence is None:
            raise DomainError(ErrorCode.INVALID_STATE, "interaction resolution event missing")
        row = ConversationEventRecord.objects.filter(
            conversation_id=conversation_id,
            sequence=answer.resolution_event_sequence,
        ).first()
        if row is None:
            raise DomainError(ErrorCode.INVALID_STATE, "interaction resolution event missing")
        event = _load(ConversationEvent, row.payload)
        if (
            event.type != "interaction_resolved"
            or getattr(event.payload, "interaction_id", None) != interaction_id
        ):
            raise DomainError(ErrorCode.INVALID_STATE, "recorded resolution event mismatch")
        return event

    async def get_interaction_request_event(
        self,
        conversation_id: UUID,
        interaction_id: UUID,
    ) -> ConversationEvent:
        return await sync_to_async(self._get_interaction_request_event, thread_sensitive=True)(
            conversation_id,
            interaction_id,
        )

    def _get_interaction_request_event(
        self,
        conversation_id: UUID,
        interaction_id: UUID,
    ) -> ConversationEvent:
        interaction = InteractionRecord.objects.filter(
            interaction_id=interaction_id,
            conversation_id=conversation_id,
        ).first()
        if interaction is None or interaction.request_event_sequence is None:
            raise DomainError(ErrorCode.INVALID_STATE, "interaction request event missing")
        row = ConversationEventRecord.objects.filter(
            conversation_id=conversation_id,
            sequence=interaction.request_event_sequence,
        ).first()
        if row is None:
            raise DomainError(ErrorCode.INVALID_STATE, "interaction request event missing")
        event = _load(ConversationEvent, row.payload)
        if (
            event.type != "interaction_requested"
            or getattr(event.payload, "interaction_id", None) != interaction_id
        ):
            raise DomainError(ErrorCode.INVALID_STATE, "recorded request event mismatch")
        return event

    async def complete_suppressed_interaction_resolution(
        self,
        interaction_id: UUID,
        published_at: datetime,
    ) -> bool:
        return await sync_to_async(
            self._complete_suppressed_interaction_resolution,
            thread_sensitive=True,
        )(interaction_id, published_at)

    @transaction.atomic
    def _complete_suppressed_interaction_resolution(
        self,
        interaction_id: UUID,
        published_at: datetime,
    ) -> bool:
        row = (
            InteractionAnswerRecord.objects.select_for_update()
            .filter(interaction_id=interaction_id)
            .first()
        )
        if row is None:
            raise DomainError(ErrorCode.INVALID_STATE, "interaction answer missing")
        if not row.answer_command_suppressed:
            return False
        if row.released_at is None:
            row.released_at = published_at
            row.save(update_fields=("released_at",))
        return True

    async def mark_interaction_policy_evaluated(
        self,
        interaction_id: UUID,
        evaluated_at: datetime,
    ) -> None:
        await sync_to_async(self._mark_policy_evaluated, thread_sensitive=True)(
            interaction_id, evaluated_at
        )

    def _mark_policy_evaluated(self, interaction_id: UUID, evaluated_at: datetime) -> None:
        InteractionRecord.objects.filter(interaction_id=interaction_id).update(
            policy_evaluated_at=evaluated_at
        )

    async def list_unevaluated_open_interactions(self) -> Sequence[tuple[UUID, UUID]]:
        return await sync_to_async(self._list_unevaluated, thread_sensitive=True)()

    def _list_unevaluated(self) -> tuple[tuple[UUID, UUID], ...]:
        rows = InteractionRecord.objects.filter(
            status__in=[InteractionStatus.PENDING.value, InteractionStatus.DRAFT.value],
            policy_evaluated_at__isnull=True,
        ).values_list("conversation_id", "interaction_id")
        return tuple((cid, iid) for cid, iid in rows)

    async def list_unreleased_resolutions(self) -> Sequence[tuple[UUID, UUID]]:
        return await sync_to_async(self._list_unreleased, thread_sensitive=True)()

    def _list_unreleased(self) -> tuple[tuple[UUID, UUID], ...]:
        rows = InteractionAnswerRecord.objects.filter(released_at__isnull=True).values_list(
            "conversation_id", "interaction_id"
        )
        return tuple((cid, iid) for cid, iid in rows if cid is not None)

    async def create_approval_rule(self, rule: ApprovalRule) -> ApprovalRuleProjection:
        return await sync_to_async(self._create_rule, thread_sensitive=True)(rule)

    def _create_rule(self, rule: ApprovalRule) -> ApprovalRuleProjection:
        ApprovalRuleRecord.objects.create(
            rule_id=rule.id,
            principal_id=rule.principal_id,
            decision=rule.decision.value,
            scope_kind=rule.scope.kind,
            scope=_json(rule.scope),
            matcher_kind=rule.matcher.kind,
            matcher=_json(rule.matcher),
            created_at=rule.created_at,
            updated_at=rule.updated_at,
        )
        return _rule_projection(rule)

    async def get_approval_rule(self, rule_id: UUID, principal_id: str) -> ApprovalRuleProjection:
        return await sync_to_async(self._get_rule, thread_sensitive=True)(rule_id, principal_id)

    def _get_rule(self, rule_id: UUID, principal_id: str) -> ApprovalRuleProjection:
        row = ApprovalRuleRecord.objects.filter(rule_id=rule_id, principal_id=principal_id).first()
        if row is None:
            raise not_found("approval rule")
        return _rule_from_row(row)

    async def replace_approval_rule(self, rule: ApprovalRule) -> ApprovalRuleProjection:
        return await sync_to_async(self._replace_rule, thread_sensitive=True)(rule)

    def _replace_rule(self, rule: ApprovalRule) -> ApprovalRuleProjection:
        updated = ApprovalRuleRecord.objects.filter(
            rule_id=rule.id, principal_id=rule.principal_id
        ).update(
            decision=rule.decision.value,
            scope_kind=rule.scope.kind,
            scope=_json(rule.scope),
            matcher_kind=rule.matcher.kind,
            matcher=_json(rule.matcher),
            updated_at=rule.updated_at,
        )
        if not updated:
            raise not_found("approval rule")
        return _rule_projection(rule)

    async def delete_approval_rule(self, rule_id: UUID, principal_id: str) -> None:
        await sync_to_async(self._delete_rule, thread_sensitive=True)(rule_id, principal_id)

    def _delete_rule(self, rule_id: UUID, principal_id: str) -> None:
        deleted, _ = ApprovalRuleRecord.objects.filter(
            rule_id=rule_id, principal_id=principal_id
        ).delete()
        if not deleted:
            raise not_found("approval rule")

    async def page_approval_rules(
        self,
        principal_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> Page[ApprovalRuleProjection]:
        return await sync_to_async(self._page_rules, thread_sensitive=True)(
            principal_id, cursor, limit
        )

    def _page_rules(
        self, principal_id: str, cursor: str | None, limit: int
    ) -> Page[ApprovalRuleProjection]:
        limit = clamp_page_limit(limit)
        qs = ApprovalRuleRecord.objects.filter(principal_id=principal_id).order_by(
            "-created_at", "-rule_id"
        )
        qs = apply_desc_datetime_cursor(qs, cursor, "created_at", "rule_id")
        rows = list(qs[: limit + 1])
        items = [_rule_from_row(r) for r in rows[:limit]]
        next_cursor = None
        if len(rows) > limit:
            last = rows[limit - 1]
            next_cursor = encode_cursor(sort=last.created_at.isoformat(), id=last.rule_id)
        return Page(items=tuple(items), next_cursor=next_cursor)

    async def list_applicable_approval_rules(self, principal_id: str) -> Sequence[ApprovalRule]:
        return await sync_to_async(self._list_rules, thread_sensitive=True)(principal_id)

    def _list_rules(self, principal_id: str) -> tuple[ApprovalRule, ...]:
        rows = ApprovalRuleRecord.objects.filter(principal_id=principal_id)
        return tuple(_rule_domain_from_row(r) for r in rows)

    async def get_interaction_audit(
        self, audit_id: UUID, principal_id: str
    ) -> InteractionAuditProjection:
        return await sync_to_async(self._get_audit, thread_sensitive=True)(audit_id, principal_id)

    def _get_audit(self, audit_id: UUID, principal_id: str) -> InteractionAuditProjection:
        row = InteractionAuditRecord.objects.filter(
            audit_id=audit_id, principal_id=principal_id
        ).first()
        if row is None:
            raise not_found("interaction audit")
        return _audit_from_row(row)

    async def page_interaction_audits(
        self,
        principal_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> Page[InteractionAuditProjection]:
        return await sync_to_async(self._page_audits, thread_sensitive=True)(
            principal_id, cursor, limit
        )

    def _page_audits(
        self, principal_id: str, cursor: str | None, limit: int
    ) -> Page[InteractionAuditProjection]:
        limit = clamp_page_limit(limit)
        qs = InteractionAuditRecord.objects.filter(principal_id=principal_id).order_by(
            "-created_at", "-audit_id"
        )
        qs = apply_desc_datetime_cursor(qs, cursor, "created_at", "audit_id")
        rows = list(qs[: limit + 1])
        items = [_audit_from_row(r) for r in rows[:limit]]
        next_cursor = None
        if len(rows) > limit:
            last = rows[limit - 1]
            next_cursor = encode_cursor(sort=last.created_at.isoformat(), id=last.audit_id)
        return Page(items=tuple(items), next_cursor=next_cursor)

    def _commit_turn_batch_sync(
        self,
        conversation_id: UUID,
        expected_version: int,
        state: ConversationState,
        events: tuple[ConversationEvent, ...],
        commands: tuple[Command, ...],
        *,
        worker_id: str | None = None,
        fence: int | None = None,
    ) -> tuple[ConversationEvent, ...]:
        """Synchronous body shared with commit_turn_batch (must be in a transaction)."""
        row = (
            ConversationAggregate.objects.select_for_update()
            .filter(conversation_id=conversation_id)
            .first()
        )
        if row is None:
            raise DomainError(ErrorCode.NOT_FOUND, "conversation not found")
        self._require_owner_on_row(row, worker_id, fence)
        if row.version != expected_version:
            raise _conflict(expected_version, row.version)
        expected_sequence = row.next_event_sequence
        for event in events:
            if event.conversation_id != conversation_id or event.sequence != expected_sequence:
                raise DomainError(ErrorCode.OPTIMISTIC_CONFLICT, "event sequence conflict")
            expected_sequence += 1
        if state.conversation.next_event_sequence != expected_sequence:
            raise DomainError(ErrorCode.OPTIMISTIC_CONFLICT, "aggregate sequence conflict")
        self._store_aggregate(row, state)
        _insert_events(conversation_id, events, state)
        for command in commands:
            CommandRecord.objects.update_or_create(
                command_id=command.id,
                defaults={
                    "conversation_id": conversation_id,
                    "idempotency_key": command.idempotency_key,
                    **self._command_values(command),
                },
            )
        from talktoharnesses.django.materialize import materialize_projections

        materialize_projections(state, events)
        return events

    async def read_retained_handoff(
        self,
        conversation_id: UUID,
        *,
        owner_id: str | None = None,
    ) -> HandoffDocument:
        return await sync_to_async(self._read_retained_handoff, thread_sensitive=True)(
            conversation_id, owner_id
        )

    def _read_retained_handoff(
        self,
        conversation_id: UUID,
        owner_id: str | None,
    ) -> HandoffDocument:
        if owner_id is not None:
            self._require_owned_conversation(conversation_id, owner_id)
        entries: list[HandoffMessage | HandoffTool] = []
        for message in MessageRecord.objects.filter(conversation_id=conversation_id).select_related(
            "turn"
        ):
            entries.append(
                HandoffMessage(
                    id=message.message_id,
                    turn_id=message.turn.turn_id,
                    role=MessageRole(message.role),
                    text=message.text,
                    interrupted=message.interrupted,
                    turn_order_index=message.turn.order_index,
                    order_index=message.order_index,
                )
            )
        for tool in ToolRecord.objects.filter(conversation_id=conversation_id).select_related(
            "turn"
        ):
            # CanonicalToolResult owns the UTF-8-safe 2 KiB tail rule; full
            # output is dropped rather than handed to another harness.
            canonical = CanonicalToolResult(
                id=tool.tool_id,
                turn_id=tool.turn.turn_id,
                tool_name=tool.tool_name,
                arguments=dict(tool.arguments),
                outcome=ToolOutcome(tool.outcome),
                exit_status=tool.exit_status,
                paths=tuple(str(path) for path in (tool.paths or [])),
                output_tail=tool.output_tail,
            )
            entries.append(
                HandoffTool(
                    **canonical.model_dump(exclude={"full_output"}),
                    turn_order_index=tool.turn.order_index,
                    order_index=tool.order_index,
                )
            )
        return HandoffDocument(entries=tuple(sorted(entries, key=handoff_sort_key)))

    async def prepare_harness_switch(self, conversation_id: UUID) -> SwitchPreparation:
        return await sync_to_async(self._prepare_harness_switch, thread_sensitive=True)(
            conversation_id
        )

    @transaction.atomic
    def _prepare_harness_switch(self, conversation_id: UUID) -> SwitchPreparation:
        row = (
            ConversationAggregate.objects.select_for_update()
            .filter(conversation_id=conversation_id)
            .first()
        )
        if row is None:
            raise DomainError(ErrorCode.NOT_FOUND, "conversation not found")
        return SwitchPreparation(
            state=_load(ConversationState, row.state),
            handoff=self._read_retained_handoff(conversation_id, None),
        )

    async def commit_harness_switch(
        self,
        conversation_id: UUID,
        expected_version: int,
        state: ConversationState,
        events: Sequence[ConversationEvent],
        *,
        command: Command,
        process: ProcessRecord | None = None,
        launch_history_entry: LaunchSnapshot | None = None,
        worker_id: str | None = None,
        fence: int | None = None,
    ) -> Sequence[ConversationEvent]:
        return await sync_to_async(self._commit_harness_switch, thread_sensitive=True)(
            conversation_id,
            expected_version,
            state,
            tuple(events),
            command,
            process,
            launch_history_entry,
            worker_id,
            fence,
        )

    @transaction.atomic
    def _commit_harness_switch(
        self,
        conversation_id: UUID,
        expected_version: int,
        state: ConversationState,
        events: tuple[ConversationEvent, ...],
        command: Command,
        process: ProcessRecord | None,
        launch_history_entry: LaunchSnapshot | None,
        worker_id: str | None = None,
        fence: int | None = None,
    ) -> tuple[ConversationEvent, ...]:
        # Binding history follows ``state.binding``: projection materialization
        # closes the previous active row and writes the accepted candidate's.
        committed = self._commit_runtime_lifecycle(
            conversation_id,
            expected_version,
            state,
            process,
            launch_history_entry,
            events,
            worker_id=worker_id,
            fence=fence,
        )
        self._settle_command(command)
        return committed

    async def commit_harness_switch_failure(
        self,
        conversation_id: UUID,
        expected_version: int,
        state: ConversationState,
        events: Sequence[ConversationEvent],
        *,
        command: Command,
        worker_id: str | None = None,
        fence: int | None = None,
    ) -> Sequence[ConversationEvent]:
        # The unchanged current binding is re-synced, never closed or replaced.
        return await self.commit_turn_batch(
            conversation_id,
            expected_version,
            state,
            events,
            (command,),
            worker_id=worker_id,
            fence=fence,
        )

    def _settle_command(self, command: Command) -> None:
        updated = CommandRecord.objects.filter(command_id=command.id).update(
            **self._command_values(command)
        )
        if not updated:
            raise DomainError(
                ErrorCode.INVALID_STATE,
                "command not found",
                details={"command_id": str(command.id)},
            )

    async def list_cleanup_conversation_ids(self) -> Sequence[UUID]:
        return await sync_to_async(self._list_cleanup_conversation_ids, thread_sensitive=True)()

    def _list_cleanup_conversation_ids(self) -> Sequence[UUID]:
        return list(
            ConversationAggregate.objects.filter(deleted_at__isnull=True)
            .order_by("conversation_id")
            .values_list("conversation_id", flat=True)
        )

    async def prune_expired_history(
        self,
        conversation_id: UUID,
        cutoff: datetime,
    ) -> PruneResult | None:
        return await sync_to_async(self._prune_expired_history, thread_sensitive=True)(
            conversation_id, cutoff
        )

    @transaction.atomic
    def _prune_expired_history(
        self,
        conversation_id: UUID,
        cutoff: datetime,
    ) -> PruneResult | None:
        from talktoharnesses.django.materialize import materialize_projections

        row = (
            ConversationAggregate.objects.select_for_update()
            .filter(conversation_id=conversation_id)
            .first()
        )
        if row is None:
            return None
        state = _load(ConversationState, row.state)
        if state.binding is None:
            return None
        if any(activity.status is ActivityStatus.RUNNING for activity in state.activities.values()):
            return None
        active = state.active_turn
        waiting_expired = (
            active is not None
            and active.status is TurnStatus.WAITING
            and (active.started_at or active.created_at) <= cutoff
        )
        if active is not None and not waiting_expired:
            return None

        expired = set(
            TurnRecord.objects.filter(
                conversation_id=conversation_id,
                status__in=_TERMINAL_TURN_STATUSES,
                completed_at__lte=cutoff,
            ).values_list("turn_id", flat=True)
        )
        if active is not None and waiting_expired:
            expired.add(active.id)
        if not expired:
            return None

        now = datetime.now(UTC)
        events: tuple[ConversationEvent, ...] = ()
        if waiting_expired:
            # Cancels the turn's open interactions and settles its command
            # before the answer could reach a session about to be invalidated.
            interrupted = interrupt_turn(state, now=now, reason="retention")
            state = interrupted.state
            events += interrupted.events

        state = self._delete_turn_history(conversation_id, state, expired)

        binding = state.binding
        assert binding is not None
        previous_native_session_id = binding.native_session_id
        rotated = rotate_session(state, now=now)
        state = rotated.state
        events += rotated.events

        self._store_aggregate(row, state)
        _insert_events(conversation_id, events, state)
        # Events of deleted turns leave valid sequence gaps; new events never
        # reuse them.
        ConversationEventRecord.objects.filter(
            conversation_id=conversation_id, turn_id__in=expired
        ).delete()
        materialize_projections(state, events)
        return PruneResult(
            conversation_id=conversation_id,
            owner_id=state.conversation.owner_id,
            binding_id=binding.id,
            previous_native_session_id=previous_native_session_id,
            configuration=binding.configuration,
            handoff=self._read_retained_handoff(conversation_id, None),
            version=state.conversation.version,
            session_rotated_events=events,
            pruned_turn_count=len(expired),
            cancelled_waiting_count=1 if waiting_expired else 0,
        )

    def _delete_turn_history(
        self,
        conversation_id: UUID,
        state: ConversationState,
        expired: set[UUID],
    ) -> ConversationState:
        """Delete every turn-owned row and drop the same turns from aggregate JSON.

        Messages, reasoning, plans, tools, and usage carry real ``TurnRecord``
        foreign keys and are removed by the cascade; rows linked by turn UUID
        are deleted explicitly.
        """
        removed_interactions = set(
            InteractionRecord.objects.filter(
                conversation_id=conversation_id, turn_id__in=expired
            ).values_list("interaction_id", flat=True)
        ) | {i.id for i in state.interactions.values() if i.turn_id in expired}
        InteractionAnswerRecord.objects.filter(interaction_id__in=removed_interactions).delete()
        InteractionRecord.objects.filter(interaction_id__in=removed_interactions).delete()
        ActivityRecord.objects.filter(
            conversation_id=conversation_id, parent_turn_id__in=expired
        ).delete()
        CommandRecord.objects.filter(
            conversation_id=conversation_id, target_turn_id__in=expired
        ).delete()
        TurnRecord.objects.filter(conversation_id=conversation_id, turn_id__in=expired).delete()
        return state.model_copy(
            update={
                "commands": {
                    command_id: command
                    for command_id, command in state.commands.items()
                    if command.target_turn_id not in expired
                },
                "interactions": {
                    interaction_id: interaction
                    for interaction_id, interaction in state.interactions.items()
                    if interaction_id not in removed_interactions
                },
                "answers": {
                    interaction_id: answer
                    for interaction_id, answer in state.answers.items()
                    if interaction_id not in removed_interactions
                },
                "activities": {
                    activity_id: activity
                    for activity_id, activity in state.activities.items()
                    if activity.parent_turn_id not in expired
                },
            }
        )

    async def commit_session_rotation(
        self,
        conversation_id: UUID,
        expected_version: int,
        *,
        native_session_id: str | None,
        launch_snapshot: LaunchSnapshot | None,
        worker_id: str | None = None,
        fence: int | None = None,
    ) -> None:
        await sync_to_async(self._commit_session_rotation, thread_sensitive=True)(
            conversation_id,
            expected_version,
            native_session_id,
            launch_snapshot,
            worker_id,
            fence,
        )

    @transaction.atomic
    def _commit_session_rotation(
        self,
        conversation_id: UUID,
        expected_version: int,
        native_session_id: str | None,
        launch_snapshot: LaunchSnapshot | None,
        worker_id: str | None = None,
        fence: int | None = None,
    ) -> None:
        row, state = self._lock_for_binding_update(
            conversation_id, expected_version, worker_id=worker_id, fence=fence
        )
        assert state.binding is not None
        binding = state.binding.model_copy(
            update={
                "native_session_id": native_session_id,
                "launch_snapshot": launch_snapshot or state.binding.launch_snapshot,
                "requires_session_recreation": False,
            }
        )
        self._store_binding_update(
            row,
            state.model_copy(
                update={
                    "binding": binding,
                    "seen_native_ids": frozenset(),
                    "seen_stream_offsets": frozenset(),
                }
            ),
        )

    async def commit_rotation_requires_recreation(
        self,
        conversation_id: UUID,
        expected_version: int,
        *,
        worker_id: str | None = None,
        fence: int | None = None,
    ) -> None:
        await sync_to_async(self._commit_rotation_requires_recreation, thread_sensitive=True)(
            conversation_id, expected_version, worker_id, fence
        )

    @transaction.atomic
    def _commit_rotation_requires_recreation(
        self,
        conversation_id: UUID,
        expected_version: int,
        worker_id: str | None = None,
        fence: int | None = None,
    ) -> None:
        row, state = self._lock_for_binding_update(
            conversation_id, expected_version, worker_id=worker_id, fence=fence
        )
        marked = mark_requires_recreation(state, now=datetime.now(UTC))
        self._store_binding_update(row, marked.state)

    def _lock_for_binding_update(
        self,
        conversation_id: UUID,
        expected_version: int,
        *,
        worker_id: str | None = None,
        fence: int | None = None,
    ) -> tuple[ConversationAggregate, ConversationState]:
        row = (
            ConversationAggregate.objects.select_for_update()
            .filter(conversation_id=conversation_id)
            .first()
        )
        if row is None:
            raise not_found("conversation")
        self._require_owner_on_row(row, worker_id, fence)
        if row.version != expected_version:
            raise _conflict(expected_version, row.version)
        state = _load(ConversationState, row.state)
        if state.binding is None:
            raise DomainError(ErrorCode.INVALID_STATE, "no binding to rotate")
        return row, state

    def _store_binding_update(
        self,
        row: ConversationAggregate,
        state: ConversationState,
    ) -> None:
        from talktoharnesses.django.materialize import sync_active_binding

        self._store_aggregate(row, state)
        sync_active_binding(state)

    async def purge_soft_deleted(self, cutoff: datetime) -> int:
        return await sync_to_async(self._purge_soft_deleted, thread_sensitive=True)(cutoff)

    def _purge_soft_deleted(self, cutoff: datetime) -> int:
        # ``<=`` so a boundary row is purged the run its cutoff equals it, and
        # a rerun with the same cutoff stays idempotent.
        rows = ConversationAggregate.objects.filter(deleted_at__lte=cutoff)
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
            "target_turn_id": command.target_turn_id,
            "recovery_attempt_id": command.recovery_attempt_id,
            "data": _json(command),
        }

    def _require_owner_on_row(
        self,
        row: ConversationAggregate,
        worker_id: str | None,
        fence: int | None,
    ) -> None:
        if worker_id is None and fence is None:
            return
        if worker_id is None or fence is None:
            raise _stale_owner(conversation_id=row.conversation_id)
        now = _db_now()
        if (
            row.runtime_worker_id != worker_id
            or row.runtime_fence != fence
            or row.runtime_lease_expires_at is None
            or row.runtime_lease_expires_at < now
        ):
            raise _stale_owner(conversation_id=row.conversation_id)

    def _require_conversation_owner(
        self,
        conversation_id: UUID,
        worker_id: str | None,
        fence: int | None,
    ) -> None:
        row = (
            ConversationAggregate.objects.select_for_update()
            .filter(conversation_id=conversation_id)
            .first()
        )
        if row is None:
            raise DomainError(ErrorCode.INVALID_STATE, "conversation not found")
        self._require_owner_on_row(row, worker_id, fence)

    def _acquire_conversation_owner(
        self,
        conversation_id: UUID,
        worker_id: str,
        *,
        lease_duration: float,
        now: datetime,
        orphan_on_takeover: bool = False,
    ) -> int | None:
        """Acquire or renew ownership. Returns fence, or None if another owner is live."""
        row = (
            ConversationAggregate.objects.select_for_update()
            .filter(conversation_id=conversation_id)
            .first()
        )
        if row is None:
            return None
        lease_expires = now + timedelta(seconds=lease_duration)
        live_other = (
            row.runtime_worker_id is not None
            and row.runtime_worker_id != worker_id
            and row.runtime_lease_expires_at is not None
            and row.runtime_lease_expires_at >= now
        )
        if live_other:
            return None
        taking_over = (
            row.runtime_worker_id is not None
            and row.runtime_worker_id != worker_id
            and (row.runtime_lease_expires_at is None or row.runtime_lease_expires_at < now)
        )
        same_worker = row.runtime_worker_id == worker_id and (
            row.runtime_lease_expires_at is None or row.runtime_lease_expires_at >= now
        )
        if same_worker:
            fence = int(row.runtime_fence)
        else:
            fence = int(row.runtime_fence) + 1
            if taking_over or orphan_on_takeover:
                mark_processes_orphaned(conversation_id, now=now)
        row.runtime_worker_id = worker_id
        row.runtime_fence = fence
        row.runtime_lease_expires_at = lease_expires
        row.save(update_fields=("runtime_worker_id", "runtime_fence", "runtime_lease_expires_at"))
        return fence

    def _worker_lease_slot(self, worker_id: str, slot: str | None) -> str:
        if connection.vendor == "sqlite":
            return _SQLITE_SUPERVISOR_SLOT
        return slot or worker_id

    async def acquire_worker_lease(
        self,
        worker_id: str,
        *,
        lease_duration: float,
        slot: str | None = None,
    ) -> None:
        await sync_to_async(self._acquire_worker_lease, thread_sensitive=True)(
            worker_id, lease_duration, slot
        )

    @transaction.atomic
    def _acquire_worker_lease(
        self,
        worker_id: str,
        lease_duration: float,
        slot: str | None,
    ) -> None:
        now = _db_now()
        lease_slot = self._worker_lease_slot(worker_id, slot)
        expires = now + timedelta(seconds=lease_duration)
        row = WorkerLeaseRecord.objects.select_for_update().filter(slot=lease_slot).first()
        if row is None:
            WorkerLeaseRecord.objects.create(
                slot=lease_slot,
                worker_id=worker_id,
                started_at=now,
                heartbeat_at=now,
                expires_at=expires,
                draining=False,
            )
            return
        if row.expires_at >= now and row.worker_id != worker_id:
            raise DomainError(
                ErrorCode.WORKER_LEASE_UNAVAILABLE,
                "worker lease unavailable",
                details={"slot": lease_slot},
            )
        if row.worker_id == worker_id and row.expires_at >= now:
            row.heartbeat_at = now
            row.expires_at = expires
            row.draining = False
            row.save(update_fields=("heartbeat_at", "expires_at", "draining"))
            return
        row.worker_id = worker_id
        row.started_at = now
        row.heartbeat_at = now
        row.expires_at = expires
        row.draining = False
        row.save(
            update_fields=("worker_id", "started_at", "heartbeat_at", "expires_at", "draining")
        )

    async def renew_worker_lease(self, worker_id: str, *, lease_duration: float) -> None:
        await sync_to_async(self._renew_worker_lease, thread_sensitive=True)(
            worker_id, lease_duration
        )

    @transaction.atomic
    def _renew_worker_lease(self, worker_id: str, lease_duration: float) -> None:
        now = _db_now()
        lease_slot = self._worker_lease_slot(worker_id, None)
        row = WorkerLeaseRecord.objects.select_for_update().filter(slot=lease_slot).first()
        if row is None or row.worker_id != worker_id or row.expires_at < now:
            raise DomainError(
                ErrorCode.WORKER_LEASE_UNAVAILABLE,
                "worker lease unavailable",
                details={"slot": lease_slot},
            )
        row.heartbeat_at = now
        row.expires_at = now + timedelta(seconds=lease_duration)
        row.save(update_fields=("heartbeat_at", "expires_at"))

    async def mark_worker_draining(self, worker_id: str) -> None:
        await sync_to_async(self._mark_worker_draining, thread_sensitive=True)(worker_id)

    @transaction.atomic
    def _mark_worker_draining(self, worker_id: str) -> None:
        lease_slot = self._worker_lease_slot(worker_id, None)
        updated = WorkerLeaseRecord.objects.filter(slot=lease_slot, worker_id=worker_id).update(
            draining=True
        )
        if not updated:
            raise DomainError(
                ErrorCode.WORKER_LEASE_UNAVAILABLE,
                "worker lease unavailable",
                details={"slot": lease_slot},
            )

    async def release_worker_lease(self, worker_id: str) -> None:
        await sync_to_async(self._release_worker_lease, thread_sensitive=True)(worker_id)

    @transaction.atomic
    def _release_worker_lease(self, worker_id: str) -> None:
        lease_slot = self._worker_lease_slot(worker_id, None)
        WorkerLeaseRecord.objects.filter(slot=lease_slot, worker_id=worker_id).delete()

    async def claim_expired_conversations(
        self,
        worker_id: str,
        limit: int,
        *,
        lease_duration: float,
        trigger: str = RecoveryTrigger.TAKEOVER.value,
    ) -> Sequence[ConversationOwnership]:
        return await sync_to_async(self._claim_expired_conversations, thread_sensitive=True)(
            worker_id, limit, lease_duration, trigger
        )

    @transaction.atomic
    def _claim_expired_conversations(
        self,
        worker_id: str,
        limit: int,
        lease_duration: float,
        trigger: str,
    ) -> tuple[ConversationOwnership, ...]:
        now = _db_now()
        lease_expires = now + timedelta(seconds=lease_duration)
        expired_command_owner = CommandRecord.objects.filter(
            conversation_id=models.OuterRef("conversation_id"),
            status__in=(
                CommandStatus.CLAIMED.value,
                CommandStatus.DELIVERY_STARTED.value,
                CommandStatus.DELIVERED.value,
            ),
        ).filter(models.Q(lease_expires_at__lt=now) | models.Q(lease_expires_at__isnull=True))
        query = (
            ConversationAggregate.objects.filter(deleted_at__isnull=True)
            .filter(
                models.Q(runtime_lease_expires_at__lt=now)
                | models.Q(
                    status__in=_ACTIVE_RECOVERY_STATUSES,
                    runtime_lease_expires_at__isnull=True,
                )
                | models.Exists(expired_command_owner)
            )
            .exclude(
                runtime_worker_id=worker_id,
                runtime_lease_expires_at__gte=now,
            )
            .order_by("conversation_id")
        )
        if connection.vendor == "postgresql":
            query = query.select_for_update(skip_locked=True)
        else:
            query = query.select_for_update()

        claimed: list[ConversationOwnership] = []
        for row in query[:limit]:
            if (
                row.runtime_worker_id is not None
                and row.runtime_worker_id != worker_id
                and row.runtime_lease_expires_at is not None
                and row.runtime_lease_expires_at >= now
            ):
                continue
            fence = int(row.runtime_fence) + 1
            mark_processes_orphaned(row.conversation_id, now=now)
            RecoveryAttemptRecord.objects.filter(
                conversation_id=row.conversation_id,
                result__isnull=True,
            ).update(
                result=RecoveryResultCode.ABANDONED.value,
                reason_code=RecoveryReasonCode.WORKER_LOST.value,
                completed_at=now,
            )
            state = _load(ConversationState, row.state)
            binding_id = (
                state.binding.id
                if state.binding is not None
                else state.conversation.current_binding_id
            )
            if binding_id is None:
                raise DomainError(
                    ErrorCode.INVALID_STATE,
                    "active conversation missing binding for recovery",
                    details={"conversation_id": str(row.conversation_id)},
                )
            attempt_id = uuid4()
            RecoveryAttemptRecord.objects.create(
                attempt_id=attempt_id,
                conversation_id=row.conversation_id,
                binding_id=binding_id,
                command_id=None,
                turn_id=None,
                worker_id=worker_id,
                fence=fence,
                trigger=trigger,
                observed_delivery_phase=ObservedDeliveryPhase.NONE.value,
                action=RecoveryAction.NO_ACTION.value,
                result=None,
                reason_code=RecoveryReasonCode.WORKER_LOST.value,
                started_at=now,
                completed_at=None,
            )
            row.runtime_worker_id = worker_id
            row.runtime_fence = fence
            row.runtime_lease_expires_at = lease_expires
            row.save(
                update_fields=(
                    "runtime_worker_id",
                    "runtime_fence",
                    "runtime_lease_expires_at",
                )
            )
            claimed.append(
                ConversationOwnership(
                    conversation_id=row.conversation_id,
                    worker_id=worker_id,
                    fence=fence,
                    lease_expires_at=lease_expires,
                    recovery_attempt_id=attempt_id,
                )
            )
        return tuple(claimed)

    async def renew_owned_conversation_leases(
        self,
        worker_id: str,
        *,
        lease_duration: float,
    ) -> Sequence[LostLease]:
        return await sync_to_async(self._renew_owned_conversation_leases, thread_sensitive=True)(
            worker_id, lease_duration
        )

    @transaction.atomic
    def _renew_owned_conversation_leases(
        self,
        worker_id: str,
        lease_duration: float,
    ) -> tuple[LostLease, ...]:
        now = _db_now()
        lease_expires = now + timedelta(seconds=lease_duration)
        rows = list(
            ConversationAggregate.objects.select_for_update()
            .filter(runtime_worker_id=worker_id)
            .order_by("conversation_id")
        )
        lost: list[LostLease] = []
        for row in rows:
            if row.runtime_lease_expires_at is None or row.runtime_lease_expires_at < now:
                lost.append(
                    LostLease(conversation_id=row.conversation_id, fence=int(row.runtime_fence))
                )
                row.runtime_worker_id = None
                row.runtime_lease_expires_at = None
                row.save(update_fields=("runtime_worker_id", "runtime_lease_expires_at"))
                continue
            row.runtime_lease_expires_at = lease_expires
            row.save(update_fields=("runtime_lease_expires_at",))
        return tuple(lost)

    async def release_conversation_lease(
        self,
        conversation_id: UUID,
        worker_id: str,
        fence: int,
    ) -> None:
        await sync_to_async(self._release_conversation_lease, thread_sensitive=True)(
            conversation_id, worker_id, fence
        )

    @transaction.atomic
    def _release_conversation_lease(
        self,
        conversation_id: UUID,
        worker_id: str,
        fence: int,
    ) -> None:
        row = (
            ConversationAggregate.objects.select_for_update()
            .filter(conversation_id=conversation_id)
            .first()
        )
        if row is None:
            raise DomainError(ErrorCode.INVALID_STATE, "conversation not found")
        self._require_owner_on_row(row, worker_id, fence)
        row.runtime_worker_id = None
        row.runtime_lease_expires_at = None
        row.save(update_fields=("runtime_worker_id", "runtime_lease_expires_at"))

    async def complete_recovery_attempt(
        self,
        attempt_id: UUID,
        *,
        result: str,
        reason_code: str,
        completed_at: datetime,
    ) -> None:
        await sync_to_async(self._complete_recovery_attempt, thread_sensitive=True)(
            attempt_id, result, reason_code, completed_at
        )

    async def commit_recovery_batch(
        self,
        conversation_id: UUID,
        expected_version: int,
        state: ConversationState,
        events: Sequence[ConversationEvent],
        commands: Sequence[Command],
        *,
        interrupted_turn_id: UUID | None,
        attempt_id: UUID | None,
        command_id: UUID | None,
        turn_id: UUID | None,
        trigger: str,
        observed_delivery_phase: str,
        action: str,
        result: str,
        reason_code: str,
        completed_at: datetime,
        worker_id: str,
        fence: int,
    ) -> Sequence[ConversationEvent]:
        return await sync_to_async(self._commit_recovery_batch, thread_sensitive=True)(
            conversation_id,
            expected_version,
            state,
            tuple(events),
            tuple(commands),
            interrupted_turn_id,
            attempt_id,
            command_id,
            turn_id,
            trigger,
            observed_delivery_phase,
            action,
            result,
            reason_code,
            completed_at,
            worker_id,
            fence,
        )

    @transaction.atomic
    def _commit_recovery_batch(
        self,
        conversation_id: UUID,
        expected_version: int,
        state: ConversationState,
        events: tuple[ConversationEvent, ...],
        commands: tuple[Command, ...],
        interrupted_turn_id: UUID | None,
        attempt_id: UUID | None,
        command_id: UUID | None,
        turn_id: UUID | None,
        trigger: str,
        observed_delivery_phase: str,
        action: str,
        result: str,
        reason_code: str,
        completed_at: datetime,
        worker_id: str,
        fence: int,
    ) -> tuple[ConversationEvent, ...]:
        row = ConversationAggregate.objects.select_for_update().get(conversation_id=conversation_id)
        self._require_owner_on_row(row, worker_id, fence)
        previous = _load(ConversationState, row.state)
        committed = self._commit_turn_batch_sync(
            conversation_id,
            expected_version,
            state,
            events,
            commands,
            worker_id=worker_id,
            fence=fence,
        )
        if interrupted_turn_id is not None:
            MessageRecord.objects.filter(
                conversation_id=conversation_id,
                turn_id=interrupted_turn_id,
                role=MessageRole.ASSISTANT.value,
                completed=False,
            ).update(interrupted=True)

        for interaction_id, answer in state.answers.items():
            interaction = state.interactions.get(interaction_id)
            if (
                interaction is None
                or interaction.status is not InteractionStatus.CANCELLED
                or interaction_id in previous.answers
                or InteractionAnswerRecord.objects.filter(interaction_id=interaction_id).exists()
            ):
                continue
            interaction_row = InteractionRecord.objects.select_for_update().get(
                interaction_id=interaction_id
            )
            resolution_sequence = next(
                event.sequence
                for event in events
                if event.type == "interaction_resolved"
                and getattr(event.payload, "interaction_id", None) == interaction_id
            )
            self._persist_interaction_answer(
                conversation_id=conversation_id,
                owner_id=state.conversation.owner_id,
                state=state,
                interaction_id=interaction_id,
                answer=answer,
                interaction_row=interaction_row,
                automatic=False,
                live_rule=None,
                provider_kind=None,
                provider_request_ids=None,
                resolution_event_sequence=resolution_sequence,
                suppress_answer_command=True,
            )

        if attempt_id is not None:
            updated = RecoveryAttemptRecord.objects.filter(
                attempt_id=attempt_id,
                conversation_id=conversation_id,
                worker_id=worker_id,
                fence=fence,
                result__isnull=True,
            ).update(
                command_id=command_id,
                turn_id=turn_id,
                trigger=trigger,
                observed_delivery_phase=observed_delivery_phase,
                action=action,
                result=result,
                reason_code=reason_code,
                completed_at=completed_at,
            )
            if not updated:
                raise _stale_owner(conversation_id=conversation_id)
        return committed

    @transaction.atomic
    def _complete_recovery_attempt(
        self,
        attempt_id: UUID,
        result: str,
        reason_code: str,
        completed_at: datetime,
    ) -> None:
        updated = RecoveryAttemptRecord.objects.filter(
            attempt_id=attempt_id,
            result__isnull=True,
        ).update(
            result=result,
            reason_code=reason_code,
            completed_at=completed_at,
        )
        if not updated:
            raise DomainError(
                ErrorCode.INVALID_STATE,
                "recovery attempt not found or already completed",
                details={"attempt_id": str(attempt_id)},
            )

    async def get_open_recovery_attempt(
        self,
        conversation_id: UUID,
        worker_id: str,
        fence: int,
    ) -> RecoveryAttempt | None:
        return await sync_to_async(self._get_open_recovery_attempt, thread_sensitive=True)(
            conversation_id, worker_id, fence
        )

    async def update_recovery_attempt(
        self,
        attempt_id: UUID,
        *,
        command_id: UUID | None,
        turn_id: UUID | None,
        trigger: str,
        observed_delivery_phase: str,
        action: str,
        reason_code: str,
        worker_id: str,
        fence: int,
    ) -> None:
        await sync_to_async(self._update_recovery_attempt, thread_sensitive=True)(
            attempt_id,
            command_id,
            turn_id,
            trigger,
            observed_delivery_phase,
            action,
            reason_code,
            worker_id,
            fence,
        )

    @transaction.atomic
    def _update_recovery_attempt(
        self,
        attempt_id: UUID,
        command_id: UUID | None,
        turn_id: UUID | None,
        trigger: str,
        observed_delivery_phase: str,
        action: str,
        reason_code: str,
        worker_id: str,
        fence: int,
    ) -> None:
        attempt = RecoveryAttemptRecord.objects.select_for_update().get(attempt_id=attempt_id)
        row = ConversationAggregate.objects.select_for_update().get(
            conversation_id=attempt.conversation_id
        )
        self._require_owner_on_row(row, worker_id, fence)
        if attempt.worker_id != worker_id or attempt.fence != fence or attempt.result is not None:
            raise _stale_owner(conversation_id=attempt.conversation_id)
        attempt.command_id = command_id
        attempt.turn_id = turn_id
        attempt.trigger = trigger
        attempt.observed_delivery_phase = observed_delivery_phase
        attempt.action = action
        attempt.reason_code = reason_code
        attempt.save(
            update_fields=(
                "command_id",
                "turn_id",
                "trigger",
                "observed_delivery_phase",
                "action",
                "reason_code",
            )
        )

    def _get_open_recovery_attempt(
        self,
        conversation_id: UUID,
        worker_id: str,
        fence: int,
    ) -> RecoveryAttempt | None:
        row = (
            RecoveryAttemptRecord.objects.filter(
                conversation_id=conversation_id,
                worker_id=worker_id,
                fence=fence,
                result__isnull=True,
            )
            .order_by("-started_at")
            .first()
        )
        if row is None:
            return None
        return RecoveryAttempt(
            id=row.attempt_id,
            conversation_id=row.conversation_id,
            binding_id=row.binding_id,
            command_id=row.command_id,
            turn_id=row.turn_id,
            worker_id=row.worker_id,
            fence=int(row.fence),
            trigger=row.trigger,
            observed_delivery_phase=row.observed_delivery_phase,
            action=row.action,
            result=row.result,
            reason_code=row.reason_code,
            started_at=row.started_at,
            completed_at=row.completed_at,
        )

    async def mark_incomplete_assistant_messages_interrupted(
        self,
        conversation_id: UUID,
        turn_id: UUID,
    ) -> None:
        await sync_to_async(
            self._mark_incomplete_assistant_messages_interrupted,
            thread_sensitive=True,
        )(conversation_id, turn_id)

    def _mark_incomplete_assistant_messages_interrupted(
        self,
        conversation_id: UUID,
        turn_id: UUID,
    ) -> None:
        MessageRecord.objects.filter(
            conversation_id=conversation_id,
            turn_id=turn_id,
            role=MessageRole.ASSISTANT.value,
            completed=False,
        ).update(interrupted=True)

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

    async def list_configured_harnesses_for_readiness(self) -> Sequence[HarnessProjection]:
        return await sync_to_async(
            self._list_configured_harnesses_for_readiness, thread_sensitive=True
        )()

    def _list_configured_harnesses_for_readiness(self) -> Sequence[HarnessProjection]:
        rows = HarnessRecord.objects.order_by("harness_id")
        return tuple(harness_from_row(row) for row in rows)

    async def has_fresh_harness_probe(
        self,
        *,
        now: datetime,
        max_age_seconds: int = 300,
    ) -> bool:
        return await sync_to_async(self._has_fresh_harness_probe, thread_sensitive=True)(
            now, max_age_seconds
        )

    def _has_fresh_harness_probe(self, now: datetime, max_age_seconds: int) -> bool:
        cutoff = now - timedelta(seconds=max_age_seconds)
        return HarnessRecord.objects.filter(last_probed_at__gt=cutoff).exists()

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
        terms = normalize_search_terms(query)
        if not terms:
            return Page(items=(), next_cursor=None)
        qs = ConversationAggregate.objects.filter(
            conversation_id__in=_full_text_matches(terms),
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
        *,
        include_deleted: bool = False,
    ) -> ConversationSnapshot:
        return await sync_to_async(self._get_conversation_snapshot, thread_sensitive=True)(
            conversation_id, owner_id, include_deleted
        )

    @transaction.atomic
    def _get_conversation_snapshot(
        self,
        conversation_id: UUID,
        owner_id: str,
        include_deleted: bool,
    ) -> ConversationSnapshot:
        query = ConversationAggregate.objects.select_for_update().filter(
            conversation_id=conversation_id,
            owner_id=owner_id,
        )
        if not include_deleted:
            query = query.filter(deleted_at__isnull=True)
        row = query.first()
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
        _insert_events(conversation_id, events, state)
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

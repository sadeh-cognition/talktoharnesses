"""Private relational storage models for the optional Django backend."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from django.conf import settings
from django.db import models


class ConversationAggregate(models.Model):
    conversation_id: models.UUIDField[UUID, UUID] = models.UUIDField(
        primary_key=True, editable=False
    )
    owner_id: models.CharField[str, str] = models.CharField(max_length=255, db_index=True)
    version: models.PositiveBigIntegerField[int, int] = models.PositiveBigIntegerField(default=0)
    next_event_sequence: models.PositiveBigIntegerField[int, int] = models.PositiveBigIntegerField(
        default=1
    )
    updated_at: models.DateTimeField[datetime, datetime] = models.DateTimeField()
    deleted_at: models.DateTimeField[datetime | None, datetime | None] = models.DateTimeField(
        null=True, blank=True
    )
    # Denormalized list/search shell fields (avoid loading full aggregate JSON).
    title: models.CharField[str, str] = models.CharField(max_length=512, default="")
    status: models.CharField[str, str] = models.CharField(max_length=32, default="idle")
    harness_kind: models.CharField[str | None, str | None] = models.CharField(
        max_length=32, null=True, blank=True
    )
    model: models.CharField[str | None, str | None] = models.CharField(
        max_length=255, null=True, blank=True
    )
    mode: models.CharField[str | None, str | None] = models.CharField(
        max_length=255, null=True, blank=True
    )
    has_pending_interactions: models.BooleanField[bool, bool] = models.BooleanField(default=False)
    pinned_at: models.DateTimeField[datetime | None, datetime | None] = models.DateTimeField(
        null=True, blank=True
    )
    archived_at: models.DateTimeField[datetime | None, datetime | None] = models.DateTimeField(
        null=True, blank=True
    )
    snoozed_until: models.DateTimeField[datetime | None, datetime | None] = models.DateTimeField(
        null=True, blank=True
    )
    latest_activity_at: models.DateTimeField[datetime | None, datetime | None] = (
        models.DateTimeField(null=True, blank=True)
    )
    # Private worker ownership / fencing (never copied into aggregate JSON).
    runtime_worker_id: models.CharField[str | None, str | None] = models.CharField(
        max_length=255, null=True, blank=True
    )
    runtime_lease_expires_at: models.DateTimeField[datetime | None, datetime | None] = (
        models.DateTimeField(null=True, blank=True)
    )
    runtime_fence: models.PositiveBigIntegerField[int, int] = models.PositiveBigIntegerField(
        default=0
    )
    state: models.JSONField[dict[str, object], dict[str, object]] = models.JSONField()

    class Meta:
        db_table = "talktoharnesses_conversation"
        indexes = [
            models.Index(
                fields=["owner_id", "-updated_at", "-conversation_id"],
                name="tth_conv_list_idx",
            ),
            models.Index(
                fields=["owner_id", "deleted_at"],
                name="tth_conv_owner_del_idx",
            ),
            models.Index(
                fields=["status", "runtime_lease_expires_at"],
                name="tth_conv_recovery_scan_idx",
            ),
        ]


class HarnessRecord(models.Model):
    harness_id: models.UUIDField[UUID, UUID] = models.UUIDField(primary_key=True, editable=False)
    owner_id: models.CharField[str, str] = models.CharField(max_length=255, db_index=True)
    name: models.CharField[str, str] = models.CharField(max_length=255)
    kind: models.CharField[str, str] = models.CharField(max_length=32)
    configuration: models.JSONField[dict[str, object], dict[str, object]] = models.JSONField()
    created_at: models.DateTimeField[datetime, datetime] = models.DateTimeField()
    last_probe: models.JSONField[dict[str, object] | None, dict[str, object] | None] = (
        models.JSONField(null=True, blank=True)
    )
    last_probed_at: models.DateTimeField[datetime | None, datetime | None] = models.DateTimeField(
        null=True, blank=True
    )

    class Meta:
        db_table = "talktoharnesses_harness"
        indexes = [
            models.Index(
                fields=["owner_id", "-created_at", "-harness_id"],
                name="tth_harness_list_idx",
            ),
        ]


class TurnRecord(models.Model):
    turn_id: models.UUIDField[UUID, UUID] = models.UUIDField(primary_key=True, editable=False)
    conversation: models.ForeignKey[ConversationAggregate, ConversationAggregate] = (
        models.ForeignKey(ConversationAggregate, on_delete=models.CASCADE)
    )
    status: models.CharField[str, str] = models.CharField(max_length=32)
    user_message_id: models.UUIDField[UUID | None, UUID | None] = models.UUIDField(
        null=True, blank=True
    )
    command_id: models.UUIDField[UUID | None, UUID | None] = models.UUIDField(null=True, blank=True)
    terminal_reason: models.TextField[str | None, str | None] = models.TextField(
        null=True, blank=True
    )
    created_at: models.DateTimeField[datetime, datetime] = models.DateTimeField()
    started_at: models.DateTimeField[datetime | None, datetime | None] = models.DateTimeField(
        null=True, blank=True
    )
    completed_at: models.DateTimeField[datetime | None, datetime | None] = models.DateTimeField(
        null=True, blank=True
    )
    # Stable canonical order within a conversation (monotonic insert counter).
    order_index: models.PositiveBigIntegerField[int, int] = models.PositiveBigIntegerField()

    class Meta:
        db_table = "talktoharnesses_turn"
        indexes = [
            models.Index(
                fields=["conversation", "-order_index", "-turn_id"],
                name="tth_turn_page_idx",
            ),
        ]


class MessageRecord(models.Model):
    message_id: models.UUIDField[UUID, UUID] = models.UUIDField(primary_key=True, editable=False)
    conversation: models.ForeignKey[ConversationAggregate, ConversationAggregate] = (
        models.ForeignKey(ConversationAggregate, on_delete=models.CASCADE)
    )
    turn: models.ForeignKey[TurnRecord, TurnRecord] = models.ForeignKey(
        TurnRecord, on_delete=models.CASCADE
    )
    role: models.CharField[str, str] = models.CharField(max_length=16)
    text: models.TextField[str, str] = models.TextField(default="")
    sequence: models.PositiveIntegerField[int, int] = models.PositiveIntegerField(default=0)
    interrupted: models.BooleanField[bool, bool] = models.BooleanField(default=False)
    completed: models.BooleanField[bool, bool] = models.BooleanField(default=False)
    created_at: models.DateTimeField[datetime, datetime] = models.DateTimeField()
    # Conversation-wide canonical order (message.sequence is chunk order within
    # the message, not conversation order). Populated from the first canonical
    # event that created the message; default eases the additive migration.
    order_index: models.PositiveBigIntegerField[int, int] = models.PositiveBigIntegerField(
        default=0
    )

    class Meta:
        db_table = "talktoharnesses_message"
        indexes = [
            models.Index(
                fields=["conversation", "created_at", "message_id"],
                name="tth_message_page_idx",
            ),
            models.Index(fields=["turn", "sequence"], name="tth_message_turn_idx"),
        ]


class ReasoningRecord(models.Model):
    reasoning_id: models.UUIDField[UUID, UUID] = models.UUIDField(primary_key=True, editable=False)
    conversation: models.ForeignKey[ConversationAggregate, ConversationAggregate] = (
        models.ForeignKey(ConversationAggregate, on_delete=models.CASCADE)
    )
    turn: models.ForeignKey[TurnRecord, TurnRecord] = models.ForeignKey(
        TurnRecord, on_delete=models.CASCADE
    )
    text: models.TextField[str, str] = models.TextField(default="")
    completed: models.BooleanField[bool, bool] = models.BooleanField(default=False)

    class Meta:
        db_table = "talktoharnesses_reasoning"


class PlanRecord(models.Model):
    plan_id: models.UUIDField[UUID, UUID] = models.UUIDField(primary_key=True, editable=False)
    conversation: models.ForeignKey[ConversationAggregate, ConversationAggregate] = (
        models.ForeignKey(ConversationAggregate, on_delete=models.CASCADE)
    )
    turn: models.ForeignKey[TurnRecord, TurnRecord] = models.ForeignKey(
        TurnRecord, on_delete=models.CASCADE
    )
    items: models.JSONField[list[object], list[object]] = models.JSONField(default=list)
    order_index: models.PositiveBigIntegerField[int, int] = models.PositiveBigIntegerField()

    class Meta:
        db_table = "talktoharnesses_plan"
        indexes = [
            models.Index(
                fields=["conversation", "-order_index", "-plan_id"],
                name="tth_plan_page_idx",
            ),
        ]


class ToolRecord(models.Model):
    tool_id: models.UUIDField[UUID, UUID] = models.UUIDField(primary_key=True, editable=False)
    conversation: models.ForeignKey[ConversationAggregate, ConversationAggregate] = (
        models.ForeignKey(ConversationAggregate, on_delete=models.CASCADE)
    )
    turn: models.ForeignKey[TurnRecord, TurnRecord] = models.ForeignKey(
        TurnRecord, on_delete=models.CASCADE
    )
    tool_name: models.CharField[str, str] = models.CharField(max_length=255)
    arguments: models.JSONField[dict[str, object], dict[str, object]] = models.JSONField(
        default=dict
    )
    outcome: models.CharField[str, str] = models.CharField(max_length=32, default="unknown")
    exit_status: models.IntegerField[int | None, int | None] = models.IntegerField(
        null=True, blank=True
    )
    paths: models.JSONField[list[str], list[str]] = models.JSONField(default=list)
    output_tail: models.TextField[str, str] = models.TextField(default="")
    full_output: models.TextField[str | None, str | None] = models.TextField(null=True, blank=True)
    order_index: models.PositiveBigIntegerField[int, int] = models.PositiveBigIntegerField()

    class Meta:
        db_table = "talktoharnesses_tool"
        indexes = [
            models.Index(
                fields=["conversation", "-order_index", "-tool_id"],
                name="tth_tool_page_idx",
            ),
        ]


class UsageRecordRow(models.Model):
    """Per-turn usage projection row (distinct from domain UsageRecord)."""

    turn: models.OneToOneField[TurnRecord, TurnRecord] = models.OneToOneField(
        TurnRecord, on_delete=models.CASCADE, primary_key=True
    )
    conversation: models.ForeignKey[ConversationAggregate, ConversationAggregate] = (
        models.ForeignKey(ConversationAggregate, on_delete=models.CASCADE)
    )
    input_tokens: models.BigIntegerField[int | None, int | None] = models.BigIntegerField(
        null=True, blank=True
    )
    output_tokens: models.BigIntegerField[int | None, int | None] = models.BigIntegerField(
        null=True, blank=True
    )
    total_tokens: models.BigIntegerField[int | None, int | None] = models.BigIntegerField(
        null=True, blank=True
    )
    cached_input_tokens: models.BigIntegerField[int | None, int | None] = models.BigIntegerField(
        null=True, blank=True
    )
    cost: models.CharField[str | None, str | None] = models.CharField(
        max_length=64, null=True, blank=True
    )

    class Meta:
        db_table = "talktoharnesses_usage"


class ActivityRecord(models.Model):
    activity_id: models.UUIDField[UUID, UUID] = models.UUIDField(primary_key=True, editable=False)
    conversation: models.ForeignKey[ConversationAggregate, ConversationAggregate] = (
        models.ForeignKey(ConversationAggregate, on_delete=models.CASCADE)
    )
    parent_turn_id: models.UUIDField[UUID, UUID] = models.UUIDField()
    parent_activity_id: models.UUIDField[UUID | None, UUID | None] = models.UUIDField(
        null=True, blank=True
    )
    status: models.CharField[str, str] = models.CharField(max_length=32)
    title: models.CharField[str | None, str | None] = models.CharField(
        max_length=512, null=True, blank=True
    )
    summary: models.TextField[str | None, str | None] = models.TextField(null=True, blank=True)
    created_at: models.DateTimeField[datetime, datetime] = models.DateTimeField()
    completed_at: models.DateTimeField[datetime | None, datetime | None] = models.DateTimeField(
        null=True, blank=True
    )

    class Meta:
        db_table = "talktoharnesses_activity"
        indexes = [
            models.Index(
                fields=["conversation", "created_at", "activity_id"],
                name="tth_activity_page_idx",
            ),
        ]


class InteractionRecord(models.Model):
    interaction_id: models.UUIDField[UUID, UUID] = models.UUIDField(
        primary_key=True, editable=False
    )
    conversation: models.ForeignKey[ConversationAggregate, ConversationAggregate] = (
        models.ForeignKey(ConversationAggregate, on_delete=models.CASCADE)
    )
    turn_id: models.UUIDField[UUID, UUID] = models.UUIDField()
    kind: models.CharField[str, str] = models.CharField(max_length=32)
    status: models.CharField[str, str] = models.CharField(max_length=32)
    request: models.JSONField[dict[str, object], dict[str, object]] = models.JSONField()
    draft: models.JSONField[dict[str, object] | None, dict[str, object] | None] = models.JSONField(
        null=True, blank=True
    )
    created_at: models.DateTimeField[datetime, datetime] = models.DateTimeField()
    # Private broker metadata (never exposed on public projections / SSE).
    provider_correlation: models.JSONField[dict[str, object] | None, dict[str, object] | None] = (
        models.JSONField(null=True, blank=True)
    )
    request_event_sequence: models.PositiveBigIntegerField[int | None, int | None] = (
        models.PositiveBigIntegerField(null=True, blank=True)
    )
    policy_evaluated_at: models.DateTimeField[datetime | None, datetime | None] = (
        models.DateTimeField(null=True, blank=True)
    )

    class Meta:
        db_table = "talktoharnesses_interaction"
        indexes = [
            models.Index(
                fields=["conversation", "created_at", "interaction_id"],
                name="tth_interaction_page_idx",
            ),
            models.Index(
                fields=["status", "policy_evaluated_at"],
                name="tth_interaction_policy_idx",
            ),
        ]


class ConversationBindingRecord(models.Model):
    """Private relational binding history (not a public model).

    Mirrors ``domain.models.ConversationHarnessBinding``. The aggregate's
    ``conversation.current_binding_id`` and active in-JSON binding remain the
    domain source used by transitions; these rows provide the transactional
    history and deletion/query integrity switching needs (one active binding
    per conversation, atomic replacement, closing the previous binding).
    """

    binding_id: models.UUIDField[UUID, UUID] = models.UUIDField(primary_key=True, editable=False)
    conversation: models.ForeignKey[ConversationAggregate, ConversationAggregate] = (
        models.ForeignKey(ConversationAggregate, on_delete=models.CASCADE)
    )
    kind: models.CharField[str, str] = models.CharField(max_length=32)
    configuration: models.JSONField[dict[str, object], dict[str, object]] = models.JSONField()
    harness_instance_id: models.UUIDField[UUID | None, UUID | None] = models.UUIDField(
        null=True, blank=True
    )
    native_session_id: models.CharField[str | None, str | None] = models.CharField(
        max_length=255, null=True, blank=True
    )
    launch_snapshot: models.JSONField[dict[str, object] | None, dict[str, object] | None] = (
        models.JSONField(null=True, blank=True)
    )
    requires_session_recreation: models.BooleanField[bool, bool] = models.BooleanField(
        default=False
    )
    is_active: models.BooleanField[bool, bool] = models.BooleanField(default=True)
    created_at: models.DateTimeField[datetime, datetime] = models.DateTimeField()
    closed_at: models.DateTimeField[datetime | None, datetime | None] = models.DateTimeField(
        null=True, blank=True
    )

    class Meta:
        db_table = "talktoharnesses_binding"
        constraints = [
            models.UniqueConstraint(
                fields=("conversation",),
                condition=models.Q(is_active=True),
                name="tth_one_active_binding",
            )
        ]
        indexes = [
            models.Index(
                fields=["conversation", "-created_at"],
                name="tth_binding_history_idx",
            ),
        ]


class SearchDocument(models.Model):
    """Sanitized normalized text used by portable substring/FTS search.

    ``normalized_text`` is the shared knowledge source built by
    ``application.search_documents``. PostgreSQL derives a stored
    ``tsvector``/GIN index and SQLite derives an FTS5 virtual table from this
    column; both remain private, vendor-specific derived indexes (see
    ``docs/phase8.md`` Work Package 4).
    """

    conversation: models.OneToOneField[ConversationAggregate, ConversationAggregate] = (
        models.OneToOneField(
            ConversationAggregate, on_delete=models.CASCADE, primary_key=True, related_name="search"
        )
    )
    owner_id: models.CharField[str, str] = models.CharField(max_length=255, db_index=True)
    normalized_text: models.TextField[str, str] = models.TextField(default="")
    updated_at: models.DateTimeField[datetime, datetime] = models.DateTimeField()

    class Meta:
        db_table = "talktoharnesses_search_document"


class ApiToken(models.Model):
    """One active HS256 bearer token per Django user (stores sha256(jti) only)."""

    user: models.OneToOneField[Any, Any] = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        primary_key=True,
        related_name="talktoharnesses_api_token",
    )
    jti_digest: models.CharField[str, str] = models.CharField(max_length=64, unique=True)
    issued_at: models.DateTimeField[datetime, datetime] = models.DateTimeField()
    expires_at: models.DateTimeField[datetime, datetime] = models.DateTimeField()

    class Meta:
        db_table = "talktoharnesses_api_token"


class RuntimeProcess(models.Model):
    process_id: models.UUIDField[UUID, UUID] = models.UUIDField(primary_key=True, editable=False)
    conversation: models.ForeignKey[ConversationAggregate, ConversationAggregate] = (
        models.ForeignKey(ConversationAggregate, on_delete=models.CASCADE)
    )
    binding_id: models.UUIDField[UUID | None, UUID | None] = models.UUIDField(null=True, blank=True)
    status: models.CharField[str, str] = models.CharField(max_length=16)
    pid: models.BigIntegerField[int | None, int | None] = models.BigIntegerField(
        null=True, blank=True
    )
    started_at: models.DateTimeField[datetime | None, datetime | None] = models.DateTimeField(
        null=True, blank=True
    )
    exited_at: models.DateTimeField[datetime | None, datetime | None] = models.DateTimeField(
        null=True, blank=True
    )
    orphaned_at: models.DateTimeField[datetime | None, datetime | None] = models.DateTimeField(
        null=True, blank=True
    )
    exit_code: models.IntegerField[int | None, int | None] = models.IntegerField(
        null=True, blank=True
    )
    redacted_stderr_tail: models.TextField[str, str] = models.TextField(default="")

    class Meta:
        db_table = "talktoharnesses_process"


class LaunchHistory(models.Model):
    process: models.OneToOneField[RuntimeProcess, RuntimeProcess] = models.OneToOneField(
        RuntimeProcess, on_delete=models.CASCADE, primary_key=True
    )
    conversation: models.ForeignKey[ConversationAggregate, ConversationAggregate] = (
        models.ForeignKey(ConversationAggregate, on_delete=models.CASCADE)
    )
    launch: models.JSONField[dict[str, object], dict[str, object]] = models.JSONField()
    created_at: models.DateTimeField[datetime, datetime] = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "talktoharnesses_launch_history"


class ConversationEventRecord(models.Model):
    event_id: models.UUIDField[UUID, UUID] = models.UUIDField(primary_key=True, editable=False)
    conversation: models.ForeignKey[ConversationAggregate, ConversationAggregate] = (
        models.ForeignKey(ConversationAggregate, on_delete=models.CASCADE)
    )
    sequence: models.PositiveBigIntegerField[int, int] = models.PositiveBigIntegerField()
    timestamp: models.DateTimeField[datetime, datetime] = models.DateTimeField()
    type: models.CharField[str, str] = models.CharField(max_length=64)
    payload: models.JSONField[dict[str, object], dict[str, object]] = models.JSONField()
    # Nullable: resolved from the validated event payload during backfill;
    # interaction- and activity-only events resolve through their projection
    # rows. Used for turn-owned retention deletion, not for replay ordering.
    turn_id: models.UUIDField[UUID | None, UUID | None] = models.UUIDField(
        null=True, blank=True, db_index=True
    )

    class Meta:
        db_table = "talktoharnesses_event"
        constraints = [
            models.UniqueConstraint(
                fields=("conversation", "sequence"),
                name="talktoharnesses_unique_event_sequence",
            )
        ]
        ordering = ("conversation_id", "sequence")


class CommandRecord(models.Model):
    command_id: models.UUIDField[UUID, UUID] = models.UUIDField(primary_key=True, editable=False)
    conversation: models.ForeignKey[ConversationAggregate, ConversationAggregate] = (
        models.ForeignKey(ConversationAggregate, on_delete=models.CASCADE)
    )
    conversation_id: UUID
    idempotency_key: models.CharField[str, str] = models.CharField(max_length=255)
    status: models.CharField[str, str] = models.CharField(max_length=32)
    worker_id: models.CharField[str | None, str | None] = models.CharField(
        max_length=255, null=True, blank=True
    )
    lease_expires_at: models.DateTimeField[datetime | None, datetime | None] = models.DateTimeField(
        null=True, blank=True
    )
    data: models.JSONField[dict[str, object], dict[str, object]] = models.JSONField()
    # Nullable: resolved from the validated command payload during backfill.
    # Used for turn-owned retention deletion (including coalesced commands).
    target_turn_id: models.UUIDField[UUID | None, UUID | None] = models.UUIDField(
        null=True, blank=True, db_index=True
    )
    recovery_attempt: models.ForeignKey[
        RecoveryAttemptRecord | None, RecoveryAttemptRecord | None
    ] = models.ForeignKey(
        "RecoveryAttemptRecord",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="commands",
    )

    class Meta:
        db_table = "talktoharnesses_command"
        constraints = [
            models.UniqueConstraint(
                fields=("conversation", "idempotency_key"),
                name="talktoharnesses_unique_command_key",
            )
        ]


class WorkerLeaseRecord(models.Model):
    """Process-wide worker lease slot (SQLite singleton or per-worker on PG)."""

    slot: models.CharField[str, str] = models.CharField(max_length=255, primary_key=True)
    worker_id: models.CharField[str, str] = models.CharField(max_length=255)
    started_at: models.DateTimeField[datetime, datetime] = models.DateTimeField()
    heartbeat_at: models.DateTimeField[datetime, datetime] = models.DateTimeField()
    expires_at: models.DateTimeField[datetime, datetime] = models.DateTimeField()
    draining: models.BooleanField[bool, bool] = models.BooleanField(default=False)

    class Meta:
        db_table = "talktoharnesses_worker_lease"


class RecoveryAttemptRecord(models.Model):
    """Append-only recovery attempt with fixed codes (no free-text diagnostics)."""

    attempt_id: models.UUIDField[UUID, UUID] = models.UUIDField(primary_key=True, editable=False)
    conversation: models.ForeignKey[ConversationAggregate, ConversationAggregate] = (
        models.ForeignKey(ConversationAggregate, on_delete=models.CASCADE)
    )
    conversation_id: UUID
    binding_id: models.UUIDField[UUID, UUID] = models.UUIDField()
    command_id: models.UUIDField[UUID | None, UUID | None] = models.UUIDField(null=True, blank=True)
    turn_id: models.UUIDField[UUID | None, UUID | None] = models.UUIDField(null=True, blank=True)
    worker_id: models.CharField[str, str] = models.CharField(max_length=255)
    fence: models.PositiveBigIntegerField[int, int] = models.PositiveBigIntegerField()
    trigger: models.CharField[str, str] = models.CharField(max_length=32)
    observed_delivery_phase: models.CharField[str, str] = models.CharField(max_length=32)
    action: models.CharField[str, str] = models.CharField(max_length=32)
    result: models.CharField[str | None, str | None] = models.CharField(
        max_length=32, null=True, blank=True
    )
    reason_code: models.CharField[str, str] = models.CharField(max_length=64)
    started_at: models.DateTimeField[datetime, datetime] = models.DateTimeField()
    completed_at: models.DateTimeField[datetime | None, datetime | None] = models.DateTimeField(
        null=True, blank=True
    )

    class Meta:
        db_table = "talktoharnesses_recovery_attempt"
        indexes = [
            models.Index(
                fields=["conversation", "-started_at"],
                name="tth_recovery_conv_started_idx",
            ),
        ]


class InteractionAnswerRecord(models.Model):
    interaction_id: models.UUIDField[UUID, UUID] = models.UUIDField(
        primary_key=True, editable=False
    )
    conversation: models.ForeignKey[ConversationAggregate | None, ConversationAggregate | None] = (
        models.ForeignKey(
            ConversationAggregate,
            on_delete=models.CASCADE,
            null=True,
            blank=True,
            related_name="interaction_answers",
        )
    )
    data: models.JSONField[dict[str, object], dict[str, object]] = models.JSONField()
    submitted_at: models.DateTimeField[datetime | None, datetime | None] = models.DateTimeField(
        null=True, blank=True
    )
    command: models.OneToOneField[CommandRecord | None, CommandRecord | None] = (
        models.OneToOneField(
            "CommandRecord",
            on_delete=models.SET_NULL,
            null=True,
            blank=True,
            related_name="interaction_answer",
        )
    )
    resolution_event_sequence: models.PositiveBigIntegerField[int | None, int | None] = (
        models.PositiveBigIntegerField(null=True, blank=True)
    )
    released_at: models.DateTimeField[datetime | None, datetime | None] = models.DateTimeField(
        null=True, blank=True
    )
    answer_command_suppressed: models.BooleanField[bool, bool] = models.BooleanField(default=False)

    class Meta:
        db_table = "talktoharnesses_interaction_answer"


class ApprovalRuleRecord(models.Model):
    rule_id: models.UUIDField[UUID, UUID] = models.UUIDField(primary_key=True, editable=False)
    principal_id: models.CharField[str, str] = models.CharField(max_length=255)
    decision: models.CharField[str, str] = models.CharField(max_length=16)
    scope_kind: models.CharField[str, str] = models.CharField(max_length=32)
    scope: models.JSONField[dict[str, object], dict[str, object]] = models.JSONField()
    matcher_kind: models.CharField[str, str] = models.CharField(max_length=32)
    matcher: models.JSONField[dict[str, object], dict[str, object]] = models.JSONField()
    created_at: models.DateTimeField[datetime, datetime] = models.DateTimeField()
    updated_at: models.DateTimeField[datetime, datetime] = models.DateTimeField()

    class Meta:
        db_table = "talktoharnesses_approval_rule"
        indexes = [
            models.Index(
                fields=["principal_id", "scope_kind"],
                name="tth_rule_principal_scope_idx",
            ),
            models.Index(
                fields=["principal_id", "-created_at", "-rule_id"],
                name="tth_rule_page_idx",
            ),
        ]


class InteractionAuditRecord(models.Model):
    audit_id: models.UUIDField[UUID, UUID] = models.UUIDField(primary_key=True, editable=False)
    principal_id: models.CharField[str, str] = models.CharField(max_length=255)
    interaction_id: models.UUIDField[UUID, UUID] = models.UUIDField()
    conversation_id: models.UUIDField[UUID, UUID] = models.UUIDField()
    turn_id: models.UUIDField[UUID, UUID] = models.UUIDField()
    kind: models.CharField[str, str] = models.CharField(max_length=32)
    decision: models.CharField[str | None, str | None] = models.CharField(
        max_length=32, null=True, blank=True
    )
    answers: models.JSONField[dict[str, object] | None, dict[str, object] | None] = (
        models.JSONField(null=True, blank=True)
    )
    automatic: models.BooleanField[bool, bool] = models.BooleanField(default=False)
    created_at: models.DateTimeField[datetime, datetime] = models.DateTimeField()
    provider_kind: models.CharField[str | None, str | None] = models.CharField(
        max_length=32, null=True, blank=True
    )
    provider_request_ids: models.JSONField[dict[str, object], dict[str, object]] = models.JSONField(
        default=dict
    )
    deciding_rule: models.ForeignKey[ApprovalRuleRecord | None, ApprovalRuleRecord | None] = (
        models.ForeignKey(
            ApprovalRuleRecord,
            on_delete=models.SET_NULL,
            null=True,
            blank=True,
            related_name="audits",
        )
    )
    deciding_rule_id_copy: models.UUIDField[UUID | None, UUID | None] = models.UUIDField(
        null=True, blank=True
    )
    rule_decision: models.CharField[str | None, str | None] = models.CharField(
        max_length=16, null=True, blank=True
    )
    rule_scope: models.JSONField[dict[str, object] | None, dict[str, object] | None] = (
        models.JSONField(null=True, blank=True)
    )
    rule_matcher: models.JSONField[dict[str, object] | None, dict[str, object] | None] = (
        models.JSONField(null=True, blank=True)
    )
    request_action: models.JSONField[dict[str, object] | None, dict[str, object] | None] = (
        models.JSONField(null=True, blank=True)
    )

    class Meta:
        db_table = "talktoharnesses_interaction_audit"
        indexes = [
            models.Index(
                fields=["principal_id", "-created_at", "-audit_id"],
                name="tth_audit_page_idx",
            ),
            models.Index(
                fields=["interaction_id"],
                name="tth_audit_interaction_idx",
            ),
        ]


# Silence unused typing import for JSONField generics under older type checkers.
_ = Any

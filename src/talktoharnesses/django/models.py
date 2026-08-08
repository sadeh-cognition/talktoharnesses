"""Private relational storage models for the optional Django backend."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

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
    state: models.JSONField[dict[str, object], dict[str, object]] = models.JSONField()

    class Meta:
        db_table = "talktoharnesses_conversation"


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
    idempotency_key: models.CharField[str, str] = models.CharField(max_length=255)
    status: models.CharField[str, str] = models.CharField(max_length=32)
    worker_id: models.CharField[str | None, str | None] = models.CharField(
        max_length=255, null=True, blank=True
    )
    lease_expires_at: models.DateTimeField[datetime | None, datetime | None] = models.DateTimeField(
        null=True, blank=True
    )
    data: models.JSONField[dict[str, object], dict[str, object]] = models.JSONField()

    class Meta:
        db_table = "talktoharnesses_command"
        constraints = [
            models.UniqueConstraint(
                fields=("conversation", "idempotency_key"),
                name="talktoharnesses_unique_command_key",
            )
        ]


class InteractionAnswerRecord(models.Model):
    interaction_id: models.UUIDField[UUID, UUID] = models.UUIDField(
        primary_key=True, editable=False
    )
    data: models.JSONField[dict[str, object], dict[str, object]] = models.JSONField()
    submitted_at: models.DateTimeField[datetime | None, datetime | None] = models.DateTimeField(
        null=True, blank=True
    )

    class Meta:
        db_table = "talktoharnesses_interaction_answer"

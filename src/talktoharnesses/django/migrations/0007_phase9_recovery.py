"""Phase 9 worker ownership, fencing, and recovery attempt schema.

Adds private conversation-owner columns, worker/recovery tables, message
completion, and process orphan timestamps. Backfills completion from validated
``assistant_message_completed`` events and converts non-empty free-text
``recovery_result`` values into fixed ``legacy_unknown`` attempts (text is
discarded). Ownership starts unclaimed. Reverse drops only Phase 9 private
schema and the completion flag; canonical history is not rewritten.
"""

from __future__ import annotations

import json
import uuid

import django.db.models.deletion
from django.db import migrations, models


def _validate(model, value):
    # Strict domain models accept serialized UUIDs/enums only through the JSON
    # validation path (same rule as DjangoPersistence._load).
    return model.model_validate_json(json.dumps(value))


def _backfill_message_completed(apps):
    from talktoharnesses.domain.events import (
        AssistantMessageCompletedPayload,
        ConversationEvent,
    )

    ConversationEventRecord = apps.get_model("talktoharnesses", "ConversationEventRecord")
    MessageRecord = apps.get_model("talktoharnesses", "MessageRecord")

    completed_ids: set[uuid.UUID] = set()
    for row in ConversationEventRecord.objects.iterator():
        event = _validate(ConversationEvent, row.payload)
        if isinstance(event.payload, AssistantMessageCompletedPayload):
            completed_ids.add(event.payload.message_id)

    if completed_ids:
        MessageRecord.objects.filter(message_id__in=completed_ids).update(completed=True)


def _backfill_recovery_results(apps):
    from talktoharnesses.domain.enums import (
        ObservedDeliveryPhase,
        RecoveryAction,
        RecoveryReasonCode,
        RecoveryResultCode,
        RecoveryTrigger,
    )
    from talktoharnesses.domain.models import Command
    from talktoharnesses.domain.transitions import ConversationState

    CommandRecord = apps.get_model("talktoharnesses", "CommandRecord")
    ConversationAggregate = apps.get_model("talktoharnesses", "ConversationAggregate")
    RecoveryAttemptRecord = apps.get_model("talktoharnesses", "RecoveryAttemptRecord")

    for row in CommandRecord.objects.iterator():
        data = dict(row.data or {})
        recovery_result = data.pop("recovery_result", None)
        if recovery_result is None:
            continue
        if not str(recovery_result).strip():
            # Drop empty legacy field; keep the rest of the command payload.
            row.data = data
            row.save(update_fields=("data",))
            continue

        aggregate = ConversationAggregate.objects.filter(
            conversation_id=row.conversation_id
        ).first()
        if aggregate is None:
            raise ValueError(
                f"command {row.command_id} references missing conversation {row.conversation_id}"
            )
        state = _validate(ConversationState, aggregate.state)
        binding_id = (
            state.binding.id
            if state.binding is not None
            else state.conversation.current_binding_id
        )
        if binding_id is None:
            raise ValueError(
                f"cannot convert recovery_result for command {row.command_id}: no binding"
            )

        attempt_id = uuid.uuid4()
        started_at = state.conversation.updated_at
        RecoveryAttemptRecord.objects.create(
            attempt_id=attempt_id,
            conversation_id=row.conversation_id,
            binding_id=binding_id,
            command_id=row.command_id,
            turn_id=data.get("target_turn_id"),
            worker_id=row.worker_id or "",
            fence=0,
            trigger=RecoveryTrigger.LEGACY.value,
            observed_delivery_phase=ObservedDeliveryPhase.OUTCOME_UNKNOWN.value,
            action=RecoveryAction.OUTCOME_UNKNOWN.value,
            result=RecoveryResultCode.LEGACY_UNKNOWN.value,
            reason_code=RecoveryReasonCode.LEGACY_UNKNOWN.value,
            started_at=started_at,
            completed_at=started_at,
        )
        data["recovery_attempt_id"] = str(attempt_id)
        command = _validate(Command, data)
        row.data = json.loads(command.model_dump_json())
        row.recovery_attempt_id = attempt_id
        row.save(update_fields=("data", "recovery_attempt_id"))


def _initialize_ownership(apps):
    ConversationAggregate = apps.get_model("talktoharnesses", "ConversationAggregate")
    ConversationAggregate.objects.update(
        runtime_worker_id=None,
        runtime_lease_expires_at=None,
        runtime_fence=0,
    )


def forwards(apps, schema_editor):
    _backfill_message_completed(apps)
    _backfill_recovery_results(apps)
    _initialize_ownership(apps)


def backwards(apps, schema_editor):
    # Schema reverse removes Phase 9 columns/tables. Canonical history is left
    # untouched; do not reintroduce free-text recovery_result values.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("talktoharnesses", "0006_phase8_fts"),
    ]

    operations = [
        migrations.AddField(
            model_name="conversationaggregate",
            name="runtime_fence",
            field=models.PositiveBigIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="conversationaggregate",
            name="runtime_lease_expires_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="conversationaggregate",
            name="runtime_worker_id",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AddField(
            model_name="messagerecord",
            name="completed",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="runtimeprocess",
            name="orphaned_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.CreateModel(
            name="WorkerLeaseRecord",
            fields=[
                ("slot", models.CharField(max_length=255, primary_key=True, serialize=False)),
                ("worker_id", models.CharField(max_length=255)),
                ("started_at", models.DateTimeField()),
                ("heartbeat_at", models.DateTimeField()),
                ("expires_at", models.DateTimeField()),
                ("draining", models.BooleanField(default=False)),
            ],
            options={
                "db_table": "talktoharnesses_worker_lease",
            },
        ),
        migrations.CreateModel(
            name="RecoveryAttemptRecord",
            fields=[
                (
                    "attempt_id",
                    models.UUIDField(editable=False, primary_key=True, serialize=False),
                ),
                ("binding_id", models.UUIDField()),
                ("command_id", models.UUIDField(blank=True, null=True)),
                ("turn_id", models.UUIDField(blank=True, null=True)),
                ("worker_id", models.CharField(max_length=255)),
                ("fence", models.PositiveBigIntegerField()),
                ("trigger", models.CharField(max_length=32)),
                ("observed_delivery_phase", models.CharField(max_length=32)),
                ("action", models.CharField(max_length=32)),
                ("result", models.CharField(blank=True, max_length=32, null=True)),
                ("reason_code", models.CharField(max_length=64)),
                ("started_at", models.DateTimeField()),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                (
                    "conversation",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="talktoharnesses.conversationaggregate",
                    ),
                ),
            ],
            options={
                "db_table": "talktoharnesses_recovery_attempt",
            },
        ),
        migrations.AddField(
            model_name="commandrecord",
            name="recovery_attempt",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="commands",
                to="talktoharnesses.recoveryattemptrecord",
            ),
        ),
        migrations.AddIndex(
            model_name="conversationaggregate",
            index=models.Index(
                fields=["status", "runtime_lease_expires_at"],
                name="tth_conv_recovery_scan_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="recoveryattemptrecord",
            index=models.Index(
                fields=["conversation", "-started_at"],
                name="tth_recovery_conv_started_idx",
            ),
        ),
        migrations.RunPython(forwards, backwards),
    ]

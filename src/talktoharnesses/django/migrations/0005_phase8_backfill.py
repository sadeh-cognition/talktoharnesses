"""Backfill Phase 8 binding history and turn/message ordering links.

Populates the rows added by ``0004_phase8_foundations`` from already-validated
aggregate state, event payloads, and command data. An invalid binding raises
rather than inventing history (``docs/phase8.md`` Work Package 1).
"""

from __future__ import annotations

import json

from django.db import migrations


def _validate(model, value):
    # Strict domain models accept serialized UUIDs/enums only through the JSON
    # validation path (same rule as DjangoPersistence._load).
    return model.model_validate_json(json.dumps(value))


def _backfill_bindings(apps):
    from talktoharnesses.domain.models import ConversationHarnessBinding

    ConversationAggregate = apps.get_model("talktoharnesses", "ConversationAggregate")
    ConversationBindingRecord = apps.get_model("talktoharnesses", "ConversationBindingRecord")
    for aggregate in ConversationAggregate.objects.all():
        data = (aggregate.state or {}).get("binding")
        if data is None:
            continue
        binding = _validate(ConversationHarnessBinding, data)
        ConversationBindingRecord.objects.update_or_create(
            binding_id=binding.id,
            defaults={
                "conversation_id": aggregate.conversation_id,
                "kind": binding.kind.value,
                "configuration": binding.configuration.model_dump(mode="json"),
                "harness_instance_id": binding.harness_instance_id,
                "native_session_id": binding.native_session_id,
                "launch_snapshot": (
                    binding.launch_snapshot.model_dump(mode="json")
                    if binding.launch_snapshot is not None
                    else None
                ),
                "requires_session_recreation": binding.requires_session_recreation,
                "is_active": binding.is_active,
                "created_at": binding.created_at,
                "closed_at": binding.closed_at,
            },
        )


def _event_turn(event, interaction_turns, activity_turns):
    from talktoharnesses.domain.events import event_turn_id

    turn_id = event_turn_id(event)
    if turn_id is not None:
        return turn_id
    payload = event.payload
    interaction_id = getattr(payload, "interaction_id", None)
    if interaction_id is not None:
        return interaction_turns.get(interaction_id)
    activity_id = getattr(payload, "activity_id", None)
    if activity_id is not None:
        return activity_turns.get(activity_id)
    return None


def _backfill_events_and_messages(apps):
    from talktoharnesses.domain.events import (
        AssistantMessageStartedPayload,
        ConversationEvent,
        TurnQueuedPayload,
    )

    ConversationAggregate = apps.get_model("talktoharnesses", "ConversationAggregate")
    ConversationEventRecord = apps.get_model("talktoharnesses", "ConversationEventRecord")
    MessageRecord = apps.get_model("talktoharnesses", "MessageRecord")
    TurnRecord = apps.get_model("talktoharnesses", "TurnRecord")
    InteractionRecord = apps.get_model("talktoharnesses", "InteractionRecord")
    ActivityRecord = apps.get_model("talktoharnesses", "ActivityRecord")

    for conversation_id in ConversationAggregate.objects.values_list("conversation_id", flat=True):
        interaction_turns = dict(
            InteractionRecord.objects.filter(conversation_id=conversation_id).values_list(
                "interaction_id", "turn_id"
            )
        )
        activity_turns = dict(
            ActivityRecord.objects.filter(conversation_id=conversation_id).values_list(
                "activity_id", "parent_turn_id"
            )
        )
        user_message_ids = dict(
            TurnRecord.objects.filter(
                conversation_id=conversation_id, user_message_id__isnull=False
            ).values_list("turn_id", "user_message_id")
        )

        creating_sequence = {}
        for row in ConversationEventRecord.objects.filter(
            conversation_id=conversation_id
        ).order_by("sequence"):
            event = _validate(ConversationEvent, row.payload)
            turn_id = _event_turn(event, interaction_turns, activity_turns)
            if turn_id != row.turn_id:
                row.turn_id = turn_id
                row.save(update_fields=("turn_id",))
            payload = event.payload
            if isinstance(payload, AssistantMessageStartedPayload):
                creating_sequence.setdefault(payload.message_id, row.sequence)
            elif isinstance(payload, TurnQueuedPayload):
                message_id = user_message_ids.get(payload.turn_id)
                if message_id is not None:
                    creating_sequence.setdefault(message_id, row.sequence)

        # Messages without a creating event keep their created order, placed
        # after the last assigned index so conversation order stays monotonic.
        last_index = 0
        for message in MessageRecord.objects.filter(conversation_id=conversation_id).order_by(
            "created_at", "message_id"
        ):
            last_index = creating_sequence.get(message.message_id, last_index + 1)
            if message.order_index != last_index:
                message.order_index = last_index
                message.save(update_fields=("order_index",))


def _backfill_commands(apps):
    from talktoharnesses.domain.models import Command

    CommandRecord = apps.get_model("talktoharnesses", "CommandRecord")
    for row in CommandRecord.objects.all():
        target_turn_id = _validate(Command, row.data).target_turn_id
        if target_turn_id != row.target_turn_id:
            row.target_turn_id = target_turn_id
            row.save(update_fields=("target_turn_id",))


def backfill(apps, schema_editor):
    _backfill_bindings(apps)
    _backfill_events_and_messages(apps)
    _backfill_commands(apps)


class Migration(migrations.Migration):
    dependencies = [
        ("talktoharnesses", "0004_phase8_foundations"),
    ]

    operations = [
        # Reversal keeps the schema; the added columns/rows are derived data.
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]

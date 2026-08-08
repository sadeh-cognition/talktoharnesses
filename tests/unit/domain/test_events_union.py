"""Event payload discriminated union strictness."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from talktoharnesses.domain import (
    ConversationEvent,
    InteractionKind,
    conversation_event_adapter,
    event_payload_adapter,
)
from talktoharnesses.domain.events import (
    ConversationMetadataChangedPayload,
    TurnCompletedPayload,
    TurnStartedPayload,
)


def test_payload_rejects_unknown_type() -> None:
    with pytest.raises(ValidationError):
        event_payload_adapter.validate_python({"type": "not_a_real_event"})


def test_payload_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        event_payload_adapter.validate_python(
            {
                "type": "turn_started",
                "turn_id": str(uuid4()),
                "unexpected": True,
            }
        )


def test_known_payload_round_trip() -> None:
    turn_id = uuid4()
    payload = TurnStartedPayload(turn_id=turn_id)
    parsed = event_payload_adapter.validate_json(payload.model_dump_json())
    assert isinstance(parsed, TurnStartedPayload)
    assert parsed.turn_id == turn_id


def test_envelope_sequence_and_type() -> None:
    event = ConversationEvent(
        conversation_id=uuid4(),
        sequence=1,
        timestamp=datetime.now(UTC),
        type="turn_completed",
        payload=TurnCompletedPayload(turn_id=uuid4(), has_assistant_message=False),
    )
    assert event.type == "turn_completed"
    assert event.model_dump()["type"] == "turn_completed"
    assert "type" in ConversationEvent.model_json_schema()["properties"]
    again = conversation_event_adapter.validate_json(event.model_dump_json())
    assert again.sequence == 1
    assert again.payload.type == "turn_completed"


def test_envelope_rejects_type_payload_mismatch() -> None:
    with pytest.raises(ValidationError):
        ConversationEvent(
            conversation_id=uuid4(),
            sequence=1,
            timestamp=datetime.now(UTC),
            type="turn_completed",
            payload=TurnStartedPayload(turn_id=uuid4()),
        )


def test_interaction_request_rejects_unknown_nested_payload() -> None:
    with pytest.raises(ValidationError):
        event_payload_adapter.validate_python(
            {
                "type": "interaction_requested",
                "turn_id": uuid4(),
                "interaction_id": uuid4(),
                "kind": InteractionKind.APPROVAL,
                "request": {"kind": "unknown", "unexpected": True},
            }
        )


def test_metadata_changed_payload_round_trip() -> None:
    payload = ConversationMetadataChangedPayload(
        archived_at=datetime.now(UTC),
        pinned_at=None,
        snoozed_until=None,
        deleted_at=None,
    )
    parsed = event_payload_adapter.validate_json(payload.model_dump_json())
    assert isinstance(parsed, ConversationMetadataChangedPayload)
    assert parsed.archived_at is not None


def test_envelope_rejects_sequence_below_one() -> None:
    with pytest.raises(ValidationError):
        ConversationEvent(
            conversation_id=uuid4(),
            sequence=0,
            timestamp=datetime.now(UTC),
            type="turn_started",
            payload=TurnStartedPayload(turn_id=uuid4()),
        )

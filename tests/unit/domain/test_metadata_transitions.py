"""Archive / pin / snooze / soft-delete pure transitions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from talktoharnesses.domain import (
    ApprovalDecision,
    ConversationStatus,
    DomainError,
    ErrorCode,
    HarnessCapabilities,
    HarnessConfiguration,
    HarnessKind,
    InteractionKind,
    TurnStatus,
    archive_conversation,
    new_conversation_state,
    pin_conversation,
    request_interaction,
    snooze_conversation,
    soft_delete_conversation,
    start_turn,
    submit_interaction_answer,
    submit_turn,
    unarchive_conversation,
    unpin_conversation,
    unsnooze_conversation,
)
from talktoharnesses.domain.events import ConversationMetadataChangedPayload
from talktoharnesses.domain.models import (
    ApprovalRequestPayload,
    ConversationHarnessBinding,
    InteractionAnswer,
    PendingInteraction,
)
from talktoharnesses.domain.transitions import ConversationState


def _now() -> datetime:
    return datetime(2026, 8, 8, 12, 0, 0, tzinfo=UTC)


def _idle() -> ConversationState:
    now = _now()
    state = new_conversation_state(
        owner_id="owner",
        now=now,
        capabilities=HarnessCapabilities(kind=HarnessKind.GROK, version="1.0.0"),
    )
    binding = ConversationHarnessBinding(
        conversation_id=state.conversation.id,
        kind=HarnessKind.GROK,
        configuration=HarnessConfiguration(kind=HarnessKind.GROK, working_directory="/tmp/ws"),
        created_at=now,
    )
    return state.model_copy(
        update={
            "binding": binding,
            "conversation": state.conversation.model_copy(
                update={"current_binding_id": binding.id}
            ),
        }
    )


def test_archive_and_unarchive() -> None:
    state = _idle()
    r = archive_conversation(state, now=_now())
    assert r.state.conversation.archived_at is not None
    assert r.state.conversation.status is ConversationStatus.ARCHIVED
    assert r.events[0].type == "conversation_metadata_changed"
    payload = r.events[0].payload
    assert isinstance(payload, ConversationMetadataChangedPayload)
    assert payload.archived_at is not None

    r2 = unarchive_conversation(r.state, now=_now())
    assert r2.state.conversation.archived_at is None
    assert r2.state.conversation.status is ConversationStatus.IDLE


def test_archive_busy_with_active_turn() -> None:
    state = _idle()
    r = submit_turn(state, prompt="hi", idempotency_key="k", now=_now())
    r = start_turn(r.state, now=_now())
    with pytest.raises(DomainError) as exc:
        archive_conversation(r.state, now=_now())
    assert exc.value.code is ErrorCode.CONVERSATION_BUSY


def test_pin_unpin_and_snooze() -> None:
    state = _idle()
    r = pin_conversation(state, now=_now())
    assert r.state.conversation.pinned_at is not None
    r = unpin_conversation(r.state, now=_now())
    assert r.state.conversation.pinned_at is None

    until = _now() + timedelta(hours=2)
    r = snooze_conversation(r.state, now=_now(), until=until)
    assert r.state.conversation.snoozed_until == until
    r = unsnooze_conversation(r.state, now=_now())
    assert r.state.conversation.snoozed_until is None


def test_soft_delete() -> None:
    state = _idle()
    r = soft_delete_conversation(state, now=_now())
    assert r.state.conversation.deleted_at is not None
    payload = r.events[0].payload
    assert isinstance(payload, ConversationMetadataChangedPayload)
    assert payload.deleted_at is not None
    with pytest.raises(DomainError) as exc:
        pin_conversation(r.state, now=_now())
    assert exc.value.code is ErrorCode.NOT_FOUND


def test_resolve_interaction_emits_resolution_without_command() -> None:
    """Phase 6: pure transition no longer creates answer_interaction command."""
    state = _idle()
    r = submit_turn(state, prompt="x", idempotency_key="a", now=_now())
    r = start_turn(r.state, now=_now())
    assert r.state.active_turn is not None
    turn_id: UUID = r.state.active_turn.id
    interaction = PendingInteraction(
        conversation_id=r.state.conversation.id,
        turn_id=turn_id,
        kind=InteractionKind.APPROVAL,
        request=ApprovalRequestPayload(summary="ok", available_decisions=tuple(ApprovalDecision)),
        created_at=_now(),
    )
    r = request_interaction(r.state, interaction, now=_now())
    assert r.state.active_turn is not None
    assert r.state.active_turn.status is TurnStatus.WAITING
    r = submit_interaction_answer(
        r.state,
        InteractionAnswer(interaction_id=interaction.id, decision=ApprovalDecision.ALLOW_ONCE),
        now=_now(),
    )
    assert r.command is None
    assert r.events[-1].type == "interaction_resolved"
    assert interaction.id in r.state.answers
    assert r.state.active_turn is not None
    assert r.state.active_turn.status is TurnStatus.RUNNING

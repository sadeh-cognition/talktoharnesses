"""Hypothesis: first-write-wins interaction resolution."""

from __future__ import annotations

from datetime import UTC, datetime

from hypothesis import given, settings
from hypothesis import strategies as st

from talktoharnesses.domain import (
    ApprovalDecision,
    InteractionKind,
    new_conversation_state,
    request_interaction,
    start_turn,
    submit_interaction_answer,
    submit_turn,
)
from talktoharnesses.domain.models import (
    ApprovalRequestPayload,
    InteractionAnswer,
    PendingInteraction,
)


@settings(max_examples=30)
@given(st.lists(st.sampled_from(list(ApprovalDecision)), min_size=1, max_size=8))
def test_first_answer_wins(decisions: list[ApprovalDecision]) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    state = new_conversation_state(owner_id="o", now=now)
    r = submit_turn(state, prompt="x", idempotency_key="k", now=now)
    r = start_turn(r.state, now=now)
    turn_id = r.state.active_turn.id  # type: ignore[union-attr]
    interaction = PendingInteraction(
        conversation_id=r.state.conversation.id,
        turn_id=turn_id,
        kind=InteractionKind.APPROVAL,
        request=ApprovalRequestPayload(summary="ok"),
        created_at=now,
    )
    r = request_interaction(r.state, interaction, now=now)
    state = r.state
    for decision in decisions:
        r = submit_interaction_answer(
            state,
            InteractionAnswer(interaction_id=interaction.id, decision=decision),
            now=now,
        )
        state = r.state
    assert state.answers[interaction.id].decision is decisions[0]

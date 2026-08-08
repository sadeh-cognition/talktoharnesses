"""Answer-shape validation and multi-interaction race pure tests."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from talktoharnesses.domain import (
    ApprovalDecision,
    ConversationState,
    DomainError,
    ErrorCode,
    InteractionKind,
    TransitionResult,
    TurnStatus,
    cancel_open_interactions,
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
    StructuredQuestionPayload,
)


def _now() -> datetime:
    return datetime(2026, 8, 8, 12, 0, 0, tzinfo=UTC)


def _running() -> TransitionResult:
    state = new_conversation_state(owner_id="o", now=_now())
    r = submit_turn(state, prompt="x", idempotency_key="k", now=_now())
    return start_turn(r.state, now=_now())


def _approval(
    state: ConversationState,
    *,
    id: UUID | None = None,
    request: ApprovalRequestPayload | None = None,
) -> PendingInteraction:
    assert state.active_turn is not None
    return PendingInteraction(
        id=id or uuid4(),
        conversation_id=state.conversation.id,
        turn_id=state.active_turn.id,
        kind=InteractionKind.APPROVAL,
        request=(
            request
            if request is not None
            else ApprovalRequestPayload(available_decisions=tuple(ApprovalDecision))
        ),
        created_at=_now(),
    )


def test_approval_rejects_structured_answers() -> None:
    r = _running()
    i = _approval(r.state)
    r = request_interaction(r.state, i, now=_now())
    with pytest.raises(DomainError) as exc:
        submit_interaction_answer(
            r.state,
            InteractionAnswer(
                interaction_id=i.id,
                decision=ApprovalDecision.ALLOW_ONCE,
                answers={"q": "a"},
            ),
            now=_now(),
        )
    assert exc.value.code is ErrorCode.INVALID_STATE


def test_approval_requires_decision() -> None:
    r = _running()
    i = _approval(r.state)
    r = request_interaction(r.state, i, now=_now())
    with pytest.raises(DomainError) as exc:
        submit_interaction_answer(
            r.state,
            InteractionAnswer(interaction_id=i.id, decision=None),
            now=_now(),
        )
    assert exc.value.code is ErrorCode.INVALID_STATE


def test_structured_question_rejects_decision() -> None:
    r = _running()
    i = PendingInteraction(
        conversation_id=r.state.conversation.id,
        turn_id=r.state.active_turn.id,  # type: ignore[union-attr]
        kind=InteractionKind.STRUCTURED_QUESTION,
        request=StructuredQuestionPayload(questions=({"id": "q1"},)),
        created_at=_now(),
    )
    r = request_interaction(r.state, i, now=_now())
    with pytest.raises(DomainError) as exc:
        submit_interaction_answer(
            r.state,
            InteractionAnswer(
                interaction_id=i.id,
                decision=ApprovalDecision.ALLOW_ONCE,
                answers={"q1": "yes"},
            ),
            now=_now(),
        )
    assert exc.value.code is ErrorCode.INVALID_STATE


def test_structured_question_requires_answers() -> None:
    r = _running()
    i = PendingInteraction(
        conversation_id=r.state.conversation.id,
        turn_id=r.state.active_turn.id,  # type: ignore[union-attr]
        kind=InteractionKind.STRUCTURED_QUESTION,
        request=StructuredQuestionPayload(questions=({"id": "q1"},)),
        created_at=_now(),
    )
    r = request_interaction(r.state, i, now=_now())
    with pytest.raises(DomainError) as exc:
        submit_interaction_answer(
            r.state,
            InteractionAnswer(interaction_id=i.id, answers=None),
            now=_now(),
        )
    assert exc.value.code is ErrorCode.INVALID_STATE


def test_structured_question_submit_succeeds() -> None:
    r = _running()
    i = PendingInteraction(
        conversation_id=r.state.conversation.id,
        turn_id=r.state.active_turn.id,  # type: ignore[union-attr]
        kind=InteractionKind.STRUCTURED_QUESTION,
        request=StructuredQuestionPayload(questions=({"id": "q1"},)),
        created_at=_now(),
    )
    r = request_interaction(r.state, i, now=_now())
    r = submit_interaction_answer(
        r.state,
        InteractionAnswer(interaction_id=i.id, answers={"q1": "yes"}),
        now=_now(),
    )
    assert r.state.answers[i.id].answers == {"q1": "yes"}
    assert r.state.active_turn is not None
    assert r.state.active_turn.status is TurnStatus.RUNNING


def test_unavailable_decision_rejected() -> None:
    r = _running()
    i = _approval(
        r.state,
        request=ApprovalRequestPayload(available_decisions=(ApprovalDecision.DENY,)),
    )
    r = request_interaction(r.state, i, now=_now())
    with pytest.raises(DomainError) as exc:
        submit_interaction_answer(
            r.state,
            InteractionAnswer(interaction_id=i.id, decision=ApprovalDecision.ALLOW_ONCE),
            now=_now(),
        )
    assert exc.value.code is ErrorCode.INVALID_STATE


def test_resolve_one_of_many_keeps_waiting() -> None:
    r = _running()
    i1 = _approval(r.state)
    i2 = _approval(r.state, id=uuid4())
    r = request_interaction(r.state, i1, now=_now())
    r = request_interaction(r.state, i2, now=_now())
    r = submit_interaction_answer(
        r.state,
        InteractionAnswer(interaction_id=i1.id, decision=ApprovalDecision.ALLOW_ONCE),
        now=_now(),
    )
    assert r.state.active_turn is not None
    assert r.state.active_turn.status is TurnStatus.WAITING
    r = submit_interaction_answer(
        r.state,
        InteractionAnswer(interaction_id=i2.id, decision=ApprovalDecision.DENY),
        now=_now(),
    )
    assert r.state.active_turn is not None
    assert r.state.active_turn.status is TurnStatus.RUNNING


def test_cancel_open_is_idempotent_when_none_open() -> None:
    r = _running()
    result = cancel_open_interactions(r.state, now=_now())
    assert result.events == ()


def test_manual_vs_automatic_flag_on_resolution() -> None:
    r = _running()
    i = _approval(r.state)
    r = request_interaction(r.state, i, now=_now())
    manual = submit_interaction_answer(
        r.state,
        InteractionAnswer(interaction_id=i.id, decision=ApprovalDecision.ALLOW_ONCE),
        now=_now(),
        automatic=False,
    )
    assert manual.events[-1].payload.automatic is False  # type: ignore[attr-defined]

    r2 = _running()
    i2 = _approval(r2.state)
    r2 = request_interaction(r2.state, i2, now=_now())
    auto = submit_interaction_answer(
        r2.state,
        InteractionAnswer(interaction_id=i2.id, decision=ApprovalDecision.DENY),
        now=_now(),
        automatic=True,
    )
    assert auto.events[-1].payload.automatic is True  # type: ignore[attr-defined]


def test_all_four_immediate_decisions_accepted() -> None:
    for decision in ApprovalDecision:
        r = _running()
        i = _approval(
            r.state,
            request=ApprovalRequestPayload(available_decisions=tuple(ApprovalDecision)),
        )
        r = request_interaction(r.state, i, now=_now())
        r = submit_interaction_answer(
            r.state,
            InteractionAnswer(interaction_id=i.id, decision=decision),
            now=_now(),
        )
        assert r.state.answers[i.id].decision is decision

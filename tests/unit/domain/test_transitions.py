"""Table tests for pure conversation transitions."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from talktoharnesses.domain import (
    ApprovalDecision,
    CommandKind,
    ConversationStatus,
    DomainError,
    ErrorCode,
    HarnessCapabilities,
    HarnessConfiguration,
    HarnessKind,
    InteractionKind,
    LaunchSnapshot,
    TurnStatus,
    apply_steer,
    cancel_queued_prompt,
    change_mode,
    close_session,
    commit_switch,
    complete_activity,
    complete_turn,
    edit_queued_prompt,
    fail_session,
    fail_switch,
    fail_turn,
    interrupt_turn,
    mark_outcome_unknown,
    new_conversation_state,
    reap_session,
    register_activity,
    request_interaction,
    resume_session,
    rotate_session,
    start_session,
    start_turn,
    submit_interaction_answer,
    submit_turn,
    update_interaction_draft,
)
from talktoharnesses.domain.models import (
    ApprovalRequestPayload,
    ConversationHarnessBinding,
    InteractionAnswer,
    PendingInteraction,
)
from talktoharnesses.domain.transitions import ConversationState


def _now() -> datetime:
    return datetime(2026, 8, 8, 12, 0, 0, tzinfo=UTC)


def _caps(*, steer: bool = False) -> HarnessCapabilities:
    return HarnessCapabilities(
        kind=HarnessKind.GROK,
        version="1.0.0",
        supports_steer=steer,
        supports_resume=True,
        supports_interrupt=True,
    )


def _binding(conversation_id: UUID) -> ConversationHarnessBinding:
    return ConversationHarnessBinding(
        conversation_id=conversation_id,
        kind=HarnessKind.GROK,
        configuration=HarnessConfiguration(
            kind=HarnessKind.GROK,
            working_directory="/tmp/ws",
            model="grok",
            mode="default",
        ),
        native_session_id="sess-1",
        created_at=_now(),
    )


def _idle(*, steer: bool = False) -> ConversationState:
    now = _now()
    state = new_conversation_state(owner_id="owner", now=now, capabilities=_caps(steer=steer))
    binding = _binding(state.conversation.id)
    return state.model_copy(
        update={
            "binding": binding,
            "conversation": state.conversation.model_copy(
                update={"current_binding_id": binding.id}
            ),
        }
    )


def test_submit_queues_when_idle() -> None:
    state = _idle()
    result = submit_turn(state, prompt="hello", idempotency_key="k1", now=_now())
    assert result.state.queued_turn is not None
    assert result.state.queued_user_text == "hello"
    assert result.events[0].payload.type == "turn_queued"
    assert result.command is not None
    assert result.state.idle_reap_eligible is False
    with pytest.raises(DomainError):
        reap_session(result.state, now=_now())


def test_idempotent_submit_returns_same_command() -> None:
    state = _idle()
    r1 = submit_turn(state, prompt="a", idempotency_key="same", now=_now())
    r2 = submit_turn(r1.state, prompt="b", idempotency_key="same", now=_now())
    assert r2.events == ()
    assert r2.command is not None and r1.command is not None
    assert r2.command.id == r1.command.id


def test_start_turn_enforces_one_active() -> None:
    state = _idle()
    r = submit_turn(state, prompt="go", idempotency_key="k", now=_now())
    r = start_turn(r.state, now=_now())
    assert r.state.active_turn is not None
    assert r.state.active_turn.status is TurnStatus.RUNNING
    assert r.state.conversation.status is ConversationStatus.RUNNING
    with pytest.raises(DomainError) as ei:
        start_turn(r.state, now=_now())
    assert ei.value.code is ErrorCode.CONVERSATION_BUSY


def test_coalesce_queued_prompts() -> None:
    state = _idle()
    r = submit_turn(state, prompt="one", idempotency_key="a", now=_now())
    r = start_turn(r.state, now=_now())
    # second submit while running without steer -> queue
    r = submit_turn(r.state, prompt="two", idempotency_key="b", now=_now())
    r = submit_turn(r.state, prompt="three", idempotency_key="c", now=_now())
    assert r.state.queued_user_text == "two\nthree"
    assert r.events[-1].payload.type == "turn_queued"
    assert r.events[-1].payload.coalesced is True  # type: ignore[attr-defined]


def test_edit_and_cancel_queued() -> None:
    state = _idle()
    r = submit_turn(state, prompt="old", idempotency_key="a", now=_now())
    r = start_turn(r.state, now=_now())
    r = submit_turn(r.state, prompt="queued", idempotency_key="b", now=_now())
    r = edit_queued_prompt(r.state, prompt="edited", now=_now())
    assert r.state.queued_user_text == "edited"
    previous_version = r.state.conversation.version
    previous_sequence = r.state.conversation.next_event_sequence
    r = cancel_queued_prompt(r.state, now=_now())
    assert r.state.queued_turn is None
    assert r.state.queued_user_text is None
    assert r.events[-1].payload.type == "turn_cancelled"
    assert r.events[-1].sequence == previous_sequence
    assert r.state.conversation.next_event_sequence == previous_sequence + 1
    assert r.state.conversation.version == previous_version + 1


def test_steer_success_and_fallback() -> None:
    state = _idle(steer=True)
    r = submit_turn(state, prompt="start", idempotency_key="a", now=_now())
    r = start_turn(r.state, now=_now())
    r = apply_steer(
        r.state,
        prompt="steer me",
        idempotency_key="s1",
        now=_now(),
        steer_succeeded=True,
    )
    assert r.events[-1].payload.type == "turn_steering"

    r = apply_steer(
        r.state,
        prompt="fallback",
        idempotency_key="s2",
        now=_now(),
        steer_succeeded=False,
    )
    assert r.state.queued_user_text == "fallback"
    assert r.events[-1].payload.type == "turn_queued"
    assert r.command is not None
    assert r.command.kind is CommandKind.SUBMIT_TURN


def test_submit_auto_steer_when_supported() -> None:
    state = _idle(steer=True)
    r = submit_turn(state, prompt="start", idempotency_key="a", now=_now())
    r = start_turn(r.state, now=_now())
    r = submit_turn(r.state, prompt="nudge", idempotency_key="b", now=_now())
    assert any(e.payload.type == "turn_steering" for e in r.events)


def test_complete_without_assistant_message() -> None:
    state = _idle()
    r = submit_turn(state, prompt="x", idempotency_key="a", now=_now())
    r = start_turn(r.state, now=_now())
    r = complete_turn(r.state, now=_now(), has_assistant_message=False)
    assert r.state.active_turn is None
    assert r.state.conversation.status is ConversationStatus.IDLE
    assert r.events[-1].payload.type == "turn_completed"
    assert r.events[-1].payload.has_assistant_message is False  # type: ignore[attr-defined]


def test_completed_turn_with_queued_work_is_not_reap_eligible() -> None:
    state = _idle()
    r = submit_turn(state, prompt="active", idempotency_key="a", now=_now())
    r = start_turn(r.state, now=_now())
    r = submit_turn(r.state, prompt="queued", idempotency_key="b", now=_now())
    r = complete_turn(r.state, now=_now())
    assert r.state.queued_turn is not None
    assert r.state.idle_reap_eligible is False
    with pytest.raises(DomainError):
        reap_session(r.state, now=_now())


def test_background_activity_after_completion() -> None:
    state = _idle()
    r = submit_turn(state, prompt="x", idempotency_key="a", now=_now())
    r = start_turn(r.state, now=_now())
    turn_id = r.state.active_turn.id  # type: ignore[union-attr]
    r = register_activity(r.state, parent_turn_id=turn_id, now=_now(), title="sub")
    r = complete_turn(r.state, now=_now())
    assert r.state.conversation.status is ConversationStatus.BACKGROUND_ACTIVE
    assert r.state.idle_reap_eligible is False
    activity_id = next(iter(r.state.activities))
    r = complete_activity(r.state, activity_id=activity_id, now=_now())
    assert r.state.conversation.status is ConversationStatus.IDLE
    assert r.state.idle_reap_eligible is True


def test_interaction_draft_and_first_write_wins() -> None:
    state = _idle()
    r = submit_turn(state, prompt="x", idempotency_key="a", now=_now())
    r = start_turn(r.state, now=_now())
    turn_id = r.state.active_turn.id  # type: ignore[union-attr]
    interaction = PendingInteraction(
        conversation_id=r.state.conversation.id,
        turn_id=turn_id,
        kind=InteractionKind.APPROVAL,
        request=ApprovalRequestPayload(
            tool_name="Bash",
            command_args=("ls",),
            available_decisions=tuple(ApprovalDecision),
        ),
        created_at=_now(),
    )
    r = request_interaction(r.state, interaction, now=_now())
    assert r.state.active_turn is not None
    assert r.state.active_turn.status is TurnStatus.WAITING
    r = update_interaction_draft(
        r.state, interaction_id=interaction.id, draft={"decision": "allow_once"}, now=_now()
    )
    first = InteractionAnswer(
        interaction_id=interaction.id,
        decision=ApprovalDecision.ALLOW_ONCE,
    )
    r = submit_interaction_answer(r.state, first, now=_now())
    assert interaction.id in r.state.answers
    assert r.command is None
    second = InteractionAnswer(
        interaction_id=interaction.id,
        decision=ApprovalDecision.DENY,
    )
    r2 = submit_interaction_answer(r.state, second, now=_now())
    assert r2.events == ()
    assert r2.state.answers[interaction.id].decision is ApprovalDecision.ALLOW_ONCE


def test_approval_with_no_advertised_decisions_rejects_answer() -> None:
    state = _idle()
    r = submit_turn(state, prompt="x", idempotency_key="a", now=_now())
    r = start_turn(r.state, now=_now())
    interaction = PendingInteraction(
        conversation_id=r.state.conversation.id,
        turn_id=r.state.active_turn.id,  # type: ignore[union-attr]
        kind=InteractionKind.APPROVAL,
        request=ApprovalRequestPayload(summary="manual only"),
        created_at=_now(),
    )
    r = request_interaction(r.state, interaction, now=_now())

    with pytest.raises(DomainError) as exc:
        submit_interaction_answer(
            r.state,
            InteractionAnswer(
                interaction_id=interaction.id,
                decision=ApprovalDecision.ALLOW_ONCE,
            ),
            now=_now(),
        )

    assert exc.value.code is ErrorCode.INVALID_STATE


def test_multiple_interactions_keep_waiting_until_last() -> None:

    state = _idle()
    r = submit_turn(state, prompt="x", idempotency_key="a", now=_now())
    r = start_turn(r.state, now=_now())
    turn_id = r.state.active_turn.id  # type: ignore[union-attr]
    i1 = PendingInteraction(
        conversation_id=r.state.conversation.id,
        turn_id=turn_id,
        kind=InteractionKind.APPROVAL,
        request=ApprovalRequestPayload(summary="one", available_decisions=tuple(ApprovalDecision)),
        created_at=_now(),
    )
    i2 = PendingInteraction(
        conversation_id=r.state.conversation.id,
        turn_id=turn_id,
        kind=InteractionKind.APPROVAL,
        request=ApprovalRequestPayload(summary="two", available_decisions=tuple(ApprovalDecision)),
        created_at=_now(),
    )
    r = request_interaction(r.state, i1, now=_now())
    r = request_interaction(r.state, i2, now=_now())
    assert r.state.active_turn is not None
    assert r.state.active_turn.status is TurnStatus.WAITING
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


def test_duplicate_request_idempotent_conflict_rejected() -> None:
    state = _idle()
    r = submit_turn(state, prompt="x", idempotency_key="a", now=_now())
    r = start_turn(r.state, now=_now())
    turn_id = r.state.active_turn.id  # type: ignore[union-attr]
    i = PendingInteraction(
        conversation_id=r.state.conversation.id,
        turn_id=turn_id,
        kind=InteractionKind.APPROVAL,
        request=ApprovalRequestPayload(summary="same"),
        created_at=_now(),
    )
    r = request_interaction(r.state, i, now=_now())
    again = request_interaction(r.state, i, now=_now())
    assert again.events == ()
    conflict = PendingInteraction(
        id=i.id,
        conversation_id=r.state.conversation.id,
        turn_id=turn_id,
        kind=InteractionKind.APPROVAL,
        request=ApprovalRequestPayload(summary="different"),
        created_at=_now(),
    )
    with pytest.raises(DomainError) as exc:
        request_interaction(r.state, conflict, now=_now())
    assert exc.value.code is ErrorCode.INVALID_STATE


def test_interrupt_cancels_open_interactions() -> None:
    state = _idle()
    r = submit_turn(state, prompt="x", idempotency_key="a", now=_now())
    r = start_turn(r.state, now=_now())
    turn_id = r.state.active_turn.id  # type: ignore[union-attr]
    i = PendingInteraction(
        conversation_id=r.state.conversation.id,
        turn_id=turn_id,
        kind=InteractionKind.APPROVAL,
        request=ApprovalRequestPayload(summary="pending"),
        created_at=_now(),
    )
    r = request_interaction(r.state, i, now=_now())
    r = interrupt_turn(r.state, now=_now(), reason="user")
    assert r.state.interactions[i.id].status.value == "cancelled"
    assert any(e.type == "interaction_resolved" for e in r.events)
    assert r.events[-1].type == "turn_interrupted"


@pytest.mark.parametrize("foreign_field", ["conversation", "turn"])
def test_interaction_for_different_aggregate_is_rejected(foreign_field: str) -> None:
    state = _idle()
    r = submit_turn(state, prompt="x", idempotency_key="a", now=_now())
    r = start_turn(r.state, now=_now())
    assert r.state.active_turn is not None
    interaction = PendingInteraction(
        conversation_id=(uuid4() if foreign_field == "conversation" else r.state.conversation.id),
        turn_id=uuid4() if foreign_field == "turn" else r.state.active_turn.id,
        kind=InteractionKind.APPROVAL,
        request=ApprovalRequestPayload(summary="ok"),
        created_at=_now(),
    )
    with pytest.raises(DomainError) as exc_info:
        request_interaction(r.state, interaction, now=_now())
    assert exc_info.value.code is ErrorCode.INVALID_STATE
    assert interaction.id not in r.state.interactions
    assert r.state.active_turn.status is TurnStatus.RUNNING


def test_interrupt_fail_outcome_unknown() -> None:
    state = _idle()
    r = submit_turn(state, prompt="x", idempotency_key="a", now=_now())
    r = start_turn(r.state, now=_now())
    r = interrupt_turn(r.state, now=_now(), reason="user")
    assert r.events[-1].payload.type == "turn_interrupted"

    r = submit_turn(r.state, prompt="y", idempotency_key="b", now=_now())
    r = start_turn(r.state, now=_now())
    r = fail_turn(r.state, now=_now(), error_code="protocol_error", message="boom")
    assert r.events[-1].payload.type == "turn_failed"

    r = submit_turn(r.state, prompt="z", idempotency_key="c", now=_now())
    r = start_turn(r.state, now=_now())
    r = mark_outcome_unknown(r.state, now=_now(), delivery_phase="delivery_started")
    assert r.events[-1].payload.type == "turn_outcome_unknown"


def test_mode_change_while_active_rejected() -> None:
    state = _idle()
    r = submit_turn(state, prompt="x", idempotency_key="a", now=_now())
    r = start_turn(r.state, now=_now())
    with pytest.raises(DomainError) as ei:
        change_mode(r.state, mode="plan", now=_now())
    assert ei.value.code is ErrorCode.MODE_CHANGE_WHILE_ACTIVE


def test_session_reap_rotate_switch() -> None:
    state = _idle()
    state = state.model_copy(
        update={
            "seen_native_ids": frozenset({"native-event"}),
            "seen_stream_offsets": frozenset({"native-session:1"}),
        }
    )
    r = reap_session(state, now=_now())
    assert r.events[-1].payload.type == "session_reaped"

    r = rotate_session(r.state, now=_now())
    assert r.events[-1].payload.type == "session_rotated"
    assert r.state.binding is not None
    assert r.state.binding.requires_session_recreation is True
    assert r.state.seen_native_ids == frozenset()
    assert r.state.seen_stream_offsets == frozenset()

    new_binding = ConversationHarnessBinding(
        conversation_id=r.state.conversation.id,
        kind=HarnessKind.CURSOR,
        configuration=HarnessConfiguration(
            kind=HarnessKind.CURSOR,
            working_directory="/tmp/ws",
        ),
        created_at=_now(),
    )
    r = commit_switch(r.state, new_binding=new_binding, now=_now())
    assert r.state.binding is not None
    assert r.state.binding.kind is HarnessKind.CURSOR
    assert r.events[-1].payload.type == "harness_switched"

    r = fail_switch(r.state, now=_now(), message="reject")
    assert r.events[-1].payload.type == "harness_switch_failed"


def _launch() -> LaunchSnapshot:
    return LaunchSnapshot(
        resolved_executable="/tmp/bin",
        harness_version="1",
        working_directory="/tmp/ws",
        model="m",
        mode="default",
        adapter_version="0",
        capabilities=_caps(),
    )


def test_start_resume_close_session() -> None:
    state = _idle()
    launch = _launch()
    r = start_session(state, now=_now(), native_session_id="n1", launch=launch)
    assert r.events[0].type == "session_started"
    assert r.state.binding is not None
    assert r.state.binding.native_session_id == "n1"
    assert r.state.binding.launch_snapshot == launch

    r = resume_session(r.state, now=_now(), native_session_id="n1", launch=launch)
    assert r.events[0].type == "session_resumed"

    r = close_session(r.state, now=_now(), reason="test")
    assert r.events[0].type == "session_closed"
    # Native ID retained after close.
    assert r.state.binding is not None
    assert r.state.binding.native_session_id == "n1"


def test_fail_session() -> None:
    state = _idle()
    r = fail_session(
        state,
        now=_now(),
        error_code="runtime_timeout",
        message="boom",
    )
    assert r.events[0].type == "session_failed"


def test_reap_blocked_while_active() -> None:
    state = _idle()
    r = submit_turn(state, prompt="x", idempotency_key="a", now=_now())
    r = start_turn(r.state, now=_now())
    with pytest.raises(DomainError) as ei:
        reap_session(r.state, now=_now())
    assert ei.value.code is ErrorCode.INVALID_STATE


def test_switch_blocked_while_active() -> None:
    state = _idle()
    r = submit_turn(state, prompt="x", idempotency_key="a", now=_now())
    r = start_turn(r.state, now=_now())
    new_binding = ConversationHarnessBinding(
        conversation_id=r.state.conversation.id,
        kind=HarnessKind.CODEX,
        configuration=HarnessConfiguration(
            kind=HarnessKind.CODEX,
            working_directory="/tmp",
        ),
        created_at=_now(),
    )
    with pytest.raises(DomainError) as ei:
        commit_switch(r.state, new_binding=new_binding, now=_now())
    assert ei.value.code is ErrorCode.CONVERSATION_BUSY


def test_edit_without_queue_errors() -> None:
    state = _idle()
    with pytest.raises(DomainError) as ei:
        edit_queued_prompt(state, prompt="x", now=_now())
    assert ei.value.code is ErrorCode.NO_QUEUED_PROMPT

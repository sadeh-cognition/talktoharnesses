"""Table tests for the pure Phase 9 recovery classifier."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from talktoharnesses.application.recovery import (
    RecoveryDecisionKind,
    classify_command,
    classify_conversation,
)
from talktoharnesses.domain.enums import (
    ActivityStatus,
    CommandKind,
    CommandStatus,
    ConversationStatus,
    HarnessKind,
    RecoveryAction,
    RecoveryReasonCode,
    TurnStatus,
)
from talktoharnesses.domain.models import (
    BackgroundActivity,
    Command,
    ConversationHarnessBinding,
    HarnessConfiguration,
    SubmitTurnPayload,
    SwitchHarnessPayload,
    Turn,
)
from talktoharnesses.domain.transitions import new_conversation_state


def _now() -> datetime:
    return datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


def _state(
    *,
    status: ConversationStatus = ConversationStatus.IDLE,
    active_turn: Turn | None = None,
    native_session_id: str | None = "native-1",
    requires_recreation: bool = False,
    activities: dict[UUID, BackgroundActivity] | None = None,
    commands: dict[UUID, Command] | None = None,
):
    binding = ConversationHarnessBinding(
        conversation_id=uuid4(),
        kind=HarnessKind.GROK,
        configuration=HarnessConfiguration(
            kind=HarnessKind.GROK,
            working_directory="/tmp/ws",
        ),
        native_session_id=native_session_id,
        requires_session_recreation=requires_recreation,
        created_at=_now(),
    )
    state = new_conversation_state(
        owner_id="owner",
        now=_now(),
        binding=binding,
    )
    conversation = state.conversation.model_copy(
        update={
            "status": status,
            "active_turn_id": active_turn.id if active_turn else None,
        }
    )
    return state.model_copy(
        update={
            "conversation": conversation,
            "active_turn": active_turn,
            "activities": activities or {},
            "commands": commands or {},
        }
    )


def _command(
    *,
    status: CommandStatus,
    kind: CommandKind = CommandKind.SUBMIT_TURN,
    delivery_started_at: datetime | None = None,
    delivered_at: datetime | None = None,
    lease_expires_at: datetime | None = None,
    turn_id: UUID | None = None,
) -> Command:
    payload: object
    if kind is CommandKind.SWITCH_HARNESS:
        payload = SwitchHarnessPayload(
            configuration=HarnessConfiguration(
                kind=HarnessKind.GROK,
                working_directory="/tmp/ws",
            ),
            harness_instance_id=uuid4(),
        )
    else:
        payload = SubmitTurnPayload(prompt="hi")
    return Command(
        conversation_id=uuid4(),
        kind=kind,
        status=status,
        idempotency_key=str(uuid4()),
        target_turn_id=turn_id,
        payload=payload,
        created_at=_now(),
        delivery_started_at=delivery_started_at,
        delivered_at=delivered_at,
        lease_expires_at=lease_expires_at,
    )


@pytest.mark.parametrize(
    ("status", "kwargs", "supports_resume", "live", "expected_kind", "expected_reason"),
    [
        (
            CommandStatus.ACCEPTED,
            {},
            True,
            False,
            RecoveryDecisionKind.LEAVE_CLAIMABLE,
            RecoveryReasonCode.NO_ACTION,
        ),
        (
            CommandStatus.CLAIMED,
            {"lease_expires_at": _now() - timedelta(seconds=1)},
            True,
            False,
            RecoveryDecisionKind.RECLAIM,
            RecoveryReasonCode.WORKER_LOST,
        ),
        (
            CommandStatus.CLAIMED,
            {
                "lease_expires_at": _now() - timedelta(seconds=1),
                "delivery_started_at": _now(),
            },
            True,
            True,
            RecoveryDecisionKind.OUTCOME_UNKNOWN,
            RecoveryReasonCode.DELIVERY_AMBIGUOUS,
        ),
        (
            CommandStatus.DELIVERY_STARTED,
            {"delivery_started_at": _now()},
            True,
            True,
            RecoveryDecisionKind.OUTCOME_UNKNOWN,
            RecoveryReasonCode.DELIVERY_AMBIGUOUS,
        ),
        (
            CommandStatus.DELIVERED,
            {"delivered_at": _now()},
            True,
            True,
            RecoveryDecisionKind.NATIVE_RESUME,
            RecoveryReasonCode.UNCHANGED_LAUNCH,
        ),
        (
            CommandStatus.DELIVERED,
            {"delivered_at": _now()},
            False,
            True,
            RecoveryDecisionKind.HANDOFF_FALLBACK,
            RecoveryReasonCode.RECOVERY_FALLBACK,
        ),
        (
            CommandStatus.DELIVERED,
            {"delivered_at": _now()},
            True,
            False,
            RecoveryDecisionKind.INVARIANT_FAILURE,
            RecoveryReasonCode.INVARIANT_FAILURE,
        ),
        (
            CommandStatus.SETTLED,
            {},
            True,
            False,
            RecoveryDecisionKind.NO_ACTION,
            RecoveryReasonCode.NO_ACTION,
        ),
        (
            CommandStatus.COALESCED,
            {},
            True,
            False,
            RecoveryDecisionKind.NO_ACTION,
            RecoveryReasonCode.NO_ACTION,
        ),
        (
            CommandStatus.OUTCOME_UNKNOWN,
            {},
            True,
            False,
            RecoveryDecisionKind.NO_ACTION,
            RecoveryReasonCode.NO_ACTION,
        ),
        (
            CommandStatus.CLAIMED,
            {"lease_expires_at": _now() + timedelta(seconds=30)},
            True,
            False,
            RecoveryDecisionKind.NO_ACTION,
            RecoveryReasonCode.NO_ACTION,
        ),
    ],
)
def test_classify_command_table(
    status: CommandStatus,
    kwargs: dict[str, Any],
    supports_resume: bool,
    live: bool,
    expected_kind: RecoveryDecisionKind,
    expected_reason: RecoveryReasonCode,
) -> None:
    turn = None
    if live:
        turn = Turn(
            conversation_id=uuid4(),
            status=TurnStatus.RUNNING,
            created_at=_now(),
            started_at=_now(),
        )
    command = _command(status=status, turn_id=turn.id if turn else None, **kwargs)
    if turn is not None:
        turn = turn.model_copy(update={"command_id": command.id})
    state = _state(
        status=ConversationStatus.RUNNING if live else ConversationStatus.IDLE,
        active_turn=turn,
        commands={command.id: command},
    )
    decision = classify_command(
        command,
        state,
        now=_now(),
        supports_resume=supports_resume,
    )
    assert decision.kind is expected_kind
    assert decision.reason_code is expected_reason
    assert decision.command_id == command.id


def test_classify_delivered_without_native_id_falls_back() -> None:
    turn = Turn(
        conversation_id=uuid4(),
        status=TurnStatus.RUNNING,
        created_at=_now(),
        started_at=_now(),
    )
    command = _command(
        status=CommandStatus.DELIVERED,
        delivered_at=_now(),
        turn_id=turn.id,
    )
    turn = turn.model_copy(update={"command_id": command.id})
    state = _state(
        status=ConversationStatus.RUNNING,
        active_turn=turn,
        native_session_id=None,
        commands={command.id: command},
    )
    decision = classify_command(command, state, now=_now(), supports_resume=True)
    assert decision.kind is RecoveryDecisionKind.HANDOFF_FALLBACK
    assert decision.action is RecoveryAction.HANDOFF_FALLBACK


def test_ambiguous_secondary_command_precedes_delivered_root() -> None:
    turn = Turn(
        conversation_id=uuid4(),
        status=TurnStatus.RUNNING,
        created_at=_now(),
        started_at=_now(),
    )
    root = _command(
        status=CommandStatus.DELIVERED,
        delivered_at=_now(),
        turn_id=turn.id,
    )
    ambiguous = _command(
        status=CommandStatus.DELIVERY_STARTED,
        delivery_started_at=_now(),
        turn_id=turn.id,
    )
    turn = turn.model_copy(update={"command_id": root.id})
    state = _state(
        status=ConversationStatus.RUNNING,
        active_turn=turn,
        commands={root.id: root, ambiguous.id: ambiguous},
    )

    decisions = classify_conversation(state, now=_now(), supports_resume=True)

    assert decisions[0].kind is RecoveryDecisionKind.OUTCOME_UNKNOWN
    assert decisions[0].command_id == ambiguous.id


def test_classify_background_activity_counts_as_live() -> None:
    activity_id = uuid4()
    command = _command(status=CommandStatus.DELIVERED, delivered_at=_now())
    state = _state(
        status=ConversationStatus.BACKGROUND_ACTIVE,
        activities={
            activity_id: BackgroundActivity(
                id=activity_id,
                conversation_id=command.conversation_id,
                parent_turn_id=uuid4(),
                status=ActivityStatus.RUNNING,
                created_at=_now(),
            )
        },
        commands={command.id: command},
    )
    decision = classify_command(command, state, now=_now(), supports_resume=True)
    assert decision.kind is RecoveryDecisionKind.NATIVE_RESUME


def test_classify_delivered_requires_recreation_falls_back() -> None:
    turn = Turn(
        conversation_id=uuid4(),
        status=TurnStatus.RUNNING,
        created_at=_now(),
        started_at=_now(),
    )
    command = _command(
        status=CommandStatus.DELIVERED,
        delivered_at=_now(),
        turn_id=turn.id,
    )
    turn = turn.model_copy(update={"command_id": command.id})
    state = _state(
        status=ConversationStatus.RUNNING,
        active_turn=turn,
        requires_recreation=True,
        commands={command.id: command},
    )
    decision = classify_command(command, state, now=_now(), supports_resume=True)
    assert decision.kind is RecoveryDecisionKind.HANDOFF_FALLBACK
    assert decision.reason_code is RecoveryReasonCode.RECOVERY_FALLBACK


def test_classify_switch_delivery_started_is_ambiguous() -> None:
    command = _command(
        status=CommandStatus.DELIVERY_STARTED,
        kind=CommandKind.SWITCH_HARNESS,
        delivery_started_at=_now(),
    )
    state = _state(commands={command.id: command})
    decision = classify_command(command, state, now=_now(), supports_resume=True)
    assert decision.kind is RecoveryDecisionKind.OUTCOME_UNKNOWN
    assert decision.reason_code is RecoveryReasonCode.DELIVERY_AMBIGUOUS


def test_classify_conversation_idle_is_no_action() -> None:
    from talktoharnesses.application.recovery import classify_conversation

    state = _state(status=ConversationStatus.IDLE)
    decisions = classify_conversation(state, now=_now(), supports_resume=True)
    assert len(decisions) == 1
    assert decisions[0].kind is RecoveryDecisionKind.NO_ACTION


def test_classify_conversation_live_without_command_resumes() -> None:
    from talktoharnesses.application.recovery import classify_conversation

    turn = Turn(
        conversation_id=uuid4(),
        status=TurnStatus.WAITING,
        created_at=_now(),
        started_at=_now(),
    )
    state = _state(
        status=ConversationStatus.WAITING,
        active_turn=turn,
    )
    decisions = classify_conversation(state, now=_now(), supports_resume=True)
    assert len(decisions) == 1
    assert decisions[0].kind is RecoveryDecisionKind.NATIVE_RESUME
    assert decisions[0].turn_id == turn.id

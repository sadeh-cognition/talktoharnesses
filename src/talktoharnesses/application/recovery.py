"""Pure durable-state recovery classifier (Phase 9).

Adapters and repositories must not reimplement this table. The coordinator and
tests share these results.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from talktoharnesses.domain.enums import (
    ActivityStatus,
    CommandKind,
    CommandStatus,
    ConversationStatus,
    ObservedDeliveryPhase,
    RecoveryAction,
    RecoveryReasonCode,
    TurnStatus,
)
from talktoharnesses.domain.models import Command
from talktoharnesses.domain.transitions import ConversationState


class RecoveryDecisionKind(StrEnum):
    LEAVE_CLAIMABLE = "leave_claimable"
    RECLAIM = "reclaim"
    OUTCOME_UNKNOWN = "outcome_unknown"
    NATIVE_RESUME = "native_resume"
    HANDOFF_FALLBACK = "handoff_fallback"
    INVARIANT_FAILURE = "invariant_failure"
    NO_ACTION = "no_action"


@dataclass(frozen=True, slots=True)
class RecoveryDecision:
    """Classifier output for one command (or conversation with no command)."""

    kind: RecoveryDecisionKind
    action: RecoveryAction
    reason_code: RecoveryReasonCode
    observed_delivery_phase: ObservedDeliveryPhase
    command_id: UUID | None = None
    turn_id: UUID | None = None


def _phase_for(command: Command) -> ObservedDeliveryPhase:
    status = command.status
    if status is CommandStatus.ACCEPTED:
        return ObservedDeliveryPhase.ACCEPTED
    if status is CommandStatus.CLAIMED:
        return ObservedDeliveryPhase.CLAIMED
    if status is CommandStatus.DELIVERY_STARTED:
        return ObservedDeliveryPhase.DELIVERY_STARTED
    if status is CommandStatus.DELIVERED:
        return ObservedDeliveryPhase.DELIVERED
    if status is CommandStatus.SETTLED:
        return ObservedDeliveryPhase.SETTLED
    if status is CommandStatus.COALESCED:
        return ObservedDeliveryPhase.COALESCED
    if status is CommandStatus.OUTCOME_UNKNOWN:
        return ObservedDeliveryPhase.OUTCOME_UNKNOWN
    return ObservedDeliveryPhase.NONE


def _has_live_work(state: ConversationState) -> bool:
    if state.active_turn is not None:
        return True
    if state.queued_turn is not None:
        return True
    if any(a.status is ActivityStatus.RUNNING for a in state.activities.values()):
        return True
    return state.conversation.status in {
        ConversationStatus.RUNNING,
        ConversationStatus.WAITING,
        ConversationStatus.BACKGROUND_ACTIVE,
    }


def _resumable_native(state: ConversationState, *, supports_resume: bool) -> bool:
    binding = state.binding
    if binding is None:
        return False
    if not supports_resume:
        return False
    if not binding.native_session_id:
        return False
    return not binding.requires_session_recreation


def _ambiguous_delivery(command: Command) -> bool:
    if command.status is CommandStatus.DELIVERY_STARTED:
        return True
    return command.status is CommandStatus.CLAIMED and command.delivery_started_at is not None


def classify_command(
    command: Command,
    state: ConversationState,
    *,
    now: datetime,
    supports_resume: bool,
) -> RecoveryDecision:
    """Classify recovery for one durable command against conversation state."""
    phase = _phase_for(command)
    turn_id = command.target_turn_id
    if state.active_turn is not None and state.active_turn.command_id == command.id:
        turn_id = state.active_turn.id

    if command.status in {
        CommandStatus.SETTLED,
        CommandStatus.COALESCED,
        CommandStatus.OUTCOME_UNKNOWN,
    }:
        return RecoveryDecision(
            kind=RecoveryDecisionKind.NO_ACTION,
            action=RecoveryAction.NO_ACTION,
            reason_code=RecoveryReasonCode.NO_ACTION,
            observed_delivery_phase=phase,
            command_id=command.id,
            turn_id=turn_id,
        )

    if command.status is CommandStatus.ACCEPTED:
        return RecoveryDecision(
            kind=RecoveryDecisionKind.LEAVE_CLAIMABLE,
            action=RecoveryAction.NO_ACTION,
            reason_code=RecoveryReasonCode.NO_ACTION,
            observed_delivery_phase=phase,
            command_id=command.id,
            turn_id=turn_id,
        )

    if (
        command.status is CommandStatus.CLAIMED
        and command.delivery_started_at is None
        and (command.lease_expires_at is None or command.lease_expires_at < now)
    ):
        return RecoveryDecision(
            kind=RecoveryDecisionKind.RECLAIM,
            action=RecoveryAction.RECLAIM,
            reason_code=RecoveryReasonCode.WORKER_LOST,
            observed_delivery_phase=phase,
            command_id=command.id,
            turn_id=turn_id,
        )

    if _ambiguous_delivery(command):
        return RecoveryDecision(
            kind=RecoveryDecisionKind.OUTCOME_UNKNOWN,
            action=RecoveryAction.OUTCOME_UNKNOWN,
            reason_code=RecoveryReasonCode.DELIVERY_AMBIGUOUS,
            observed_delivery_phase=phase,
            command_id=command.id,
            turn_id=turn_id,
        )

    if command.status is CommandStatus.DELIVERED:
        live = _has_live_work(state)
        if not live:
            return RecoveryDecision(
                kind=RecoveryDecisionKind.INVARIANT_FAILURE,
                action=RecoveryAction.INVARIANT_FAILURE,
                reason_code=RecoveryReasonCode.INVARIANT_FAILURE,
                observed_delivery_phase=phase,
                command_id=command.id,
                turn_id=turn_id,
            )
        if _resumable_native(state, supports_resume=supports_resume):
            return RecoveryDecision(
                kind=RecoveryDecisionKind.NATIVE_RESUME,
                action=RecoveryAction.NATIVE_RESUME,
                reason_code=RecoveryReasonCode.UNCHANGED_LAUNCH,
                observed_delivery_phase=phase,
                command_id=command.id,
                turn_id=turn_id,
            )
        return RecoveryDecision(
            kind=RecoveryDecisionKind.HANDOFF_FALLBACK,
            action=RecoveryAction.HANDOFF_FALLBACK,
            reason_code=RecoveryReasonCode.RECOVERY_FALLBACK,
            observed_delivery_phase=phase,
            command_id=command.id,
            turn_id=turn_id,
        )

    # Non-expired claimed without delivery marker: still owned; leave alone.
    if command.status is CommandStatus.CLAIMED:
        return RecoveryDecision(
            kind=RecoveryDecisionKind.NO_ACTION,
            action=RecoveryAction.NO_ACTION,
            reason_code=RecoveryReasonCode.NO_ACTION,
            observed_delivery_phase=phase,
            command_id=command.id,
            turn_id=turn_id,
        )

    return RecoveryDecision(
        kind=RecoveryDecisionKind.INVARIANT_FAILURE,
        action=RecoveryAction.INVARIANT_FAILURE,
        reason_code=RecoveryReasonCode.INVARIANT_FAILURE,
        observed_delivery_phase=phase,
        command_id=command.id,
        turn_id=turn_id,
    )


def classify_conversation(
    state: ConversationState,
    *,
    now: datetime,
    supports_resume: bool,
) -> tuple[RecoveryDecision, ...]:
    """Classify every non-terminal or in-flight command plus live-work fallback."""
    decisions: list[RecoveryDecision] = []
    interesting = [
        c
        for c in state.commands.values()
        if c.status
        not in {
            CommandStatus.SETTLED,
            CommandStatus.COALESCED,
        }
        or (
            c.status is CommandStatus.OUTCOME_UNKNOWN
            # still report no_action when inspected
        )
    ]
    # Ambiguous secondary commands terminalize the whole conversation before
    # a delivered root command is allowed to resume it.
    interesting.sort(
        key=lambda c: (
            0
            if _ambiguous_delivery(c)
            else 1
            if state.active_turn is not None and state.active_turn.command_id == c.id
            else 2
            if c.status is CommandStatus.DELIVERED
            else 3,
            c.created_at,
        )
    )
    seen: set[UUID] = set()
    for command in interesting:
        if command.id in seen:
            continue
        seen.add(command.id)
        decisions.append(classify_command(command, state, now=now, supports_resume=supports_resume))

    if not decisions and _has_live_work(state):
        # Live conversation without a claimable command still needs inspection.
        if _resumable_native(state, supports_resume=supports_resume):
            decisions.append(
                RecoveryDecision(
                    kind=RecoveryDecisionKind.NATIVE_RESUME,
                    action=RecoveryAction.NATIVE_RESUME,
                    reason_code=RecoveryReasonCode.UNCHANGED_LAUNCH,
                    observed_delivery_phase=ObservedDeliveryPhase.NONE,
                    turn_id=state.active_turn.id if state.active_turn else None,
                )
            )
        else:
            decisions.append(
                RecoveryDecision(
                    kind=RecoveryDecisionKind.HANDOFF_FALLBACK,
                    action=RecoveryAction.HANDOFF_FALLBACK,
                    reason_code=RecoveryReasonCode.RECOVERY_FALLBACK,
                    observed_delivery_phase=ObservedDeliveryPhase.NONE,
                    turn_id=state.active_turn.id if state.active_turn else None,
                )
            )
    elif not decisions:
        decisions.append(
            RecoveryDecision(
                kind=RecoveryDecisionKind.NO_ACTION,
                action=RecoveryAction.NO_ACTION,
                reason_code=RecoveryReasonCode.NO_ACTION,
                observed_delivery_phase=ObservedDeliveryPhase.NONE,
            )
        )
    return tuple(decisions)


def is_switch_command(command: Command | None) -> bool:
    return command is not None and command.kind is CommandKind.SWITCH_HARNESS


def turn_needs_interrupt_messages(state: ConversationState, turn_id: UUID | None) -> bool:
    """True when an active/waiting turn may have incomplete assistant messages."""
    if turn_id is None:
        return False
    turn = state.active_turn
    if turn is None or turn.id != turn_id:
        return False
    return turn.status in {TurnStatus.RUNNING, TurnStatus.WAITING, TurnStatus.QUEUED}

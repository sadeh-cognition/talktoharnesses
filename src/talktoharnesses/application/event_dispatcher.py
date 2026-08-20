"""Map normalized harness events onto pure domain transitions."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from talktoharnesses.domain.enums import CommandStatus, ErrorCode
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.domain.events import (
    AssistantMessageCompletedPayload,
    AssistantMessageDeltaPayload,
    AssistantMessageStartedPayload,
    ConversationEvent,
    ConversationTitleUpdatedPayload,
    CostUpdatedPayload,
    HarnessEvent,
    InteractionRequestedPayload,
    InteractionResolvedPayload,
    PlanCreatedPayload,
    PlanUpdatedPayload,
    ReasoningCompletedPayload,
    ReasoningDeltaPayload,
    ReasoningStartedPayload,
    ToolCompletedPayload,
    ToolFailedPayload,
    ToolOutputDeltaPayload,
    ToolRequestedPayload,
    ToolStartedPayload,
    TurnCompletedPayload,
    TurnFailedPayload,
    TurnInterruptedPayload,
    TurnOutcomeUnknownPayload,
    UsageUpdatedPayload,
)
from talktoharnesses.domain.models import Command, PendingInteraction
from talktoharnesses.domain.transitions import (
    ConversationState,
    TransitionResult,
    append_events,
    apply_native_title,
    complete_turn,
    fail_turn,
    interrupt_turn,
    mark_outcome_unknown,
    remember_native_ids,
    request_interaction,
)


class DispatchResult:
    __slots__ = ("state", "events", "commands", "terminal")

    def __init__(
        self,
        state: ConversationState,
        events: tuple[ConversationEvent, ...],
        commands: tuple[Command, ...] = (),
        *,
        terminal: bool = False,
    ) -> None:
        self.state = state
        self.events = events
        self.commands = commands
        self.terminal = terminal


def dispatch_harness_event(
    state: ConversationState,
    event: HarnessEvent,
    *,
    now: datetime,
    native_ids: tuple[str, ...] = (),
    stream_offsets: tuple[str, ...] = (),
) -> DispatchResult:
    """Apply one harness event; returns updated state and durable envelopes."""
    if native_ids or stream_offsets:
        state = remember_native_ids(
            state,
            native_ids=native_ids,
            stream_offsets=stream_offsets,
        )

    if isinstance(event, ConversationTitleUpdatedPayload):
        result = apply_native_title(state, title_native=event.title_native, now=now)
        return DispatchResult(result.state, result.events)

    if isinstance(event, InteractionRequestedPayload):
        interaction = PendingInteraction(
            id=event.interaction_id,
            conversation_id=state.conversation.id,
            turn_id=event.turn_id,
            kind=event.kind,
            request=event.request,
            created_at=now,
        )
        result = request_interaction(state, interaction, now=now)
        return DispatchResult(result.state, result.events)

    if isinstance(event, TurnCompletedPayload):
        result = complete_turn(
            state,
            now=now,
            terminal_reason=event.terminal_reason,
            has_assistant_message=event.has_assistant_message,
        )
        commands = _settled_commands(result.state, state)
        return DispatchResult(result.state, result.events, commands, terminal=True)

    if isinstance(event, TurnInterruptedPayload):
        result = interrupt_turn(state, now=now, reason=event.reason)
        commands = _settled_commands(result.state, state)
        return DispatchResult(result.state, result.events, commands, terminal=True)

    if isinstance(event, TurnFailedPayload):
        result = fail_turn(
            state,
            now=now,
            error_code=event.error_code,
            message=event.message,
        )
        commands = _settled_commands(result.state, state)
        return DispatchResult(result.state, result.events, commands, terminal=True)

    if isinstance(event, TurnOutcomeUnknownPayload):
        result = mark_outcome_unknown(
            state,
            now=now,
            delivery_phase=event.delivery_phase,
            message=event.message,
        )
        commands = _settled_commands(result.state, state)
        return DispatchResult(result.state, result.events, commands, terminal=True)

    # Streaming projection events: append envelopes without dedicated transitions.
    if isinstance(
        event,
        (
            AssistantMessageStartedPayload,
            AssistantMessageDeltaPayload,
            AssistantMessageCompletedPayload,
            ReasoningStartedPayload,
            ReasoningDeltaPayload,
            ReasoningCompletedPayload,
            PlanCreatedPayload,
            PlanUpdatedPayload,
            ToolRequestedPayload,
            ToolStartedPayload,
            ToolOutputDeltaPayload,
            ToolCompletedPayload,
            ToolFailedPayload,
            UsageUpdatedPayload,
            CostUpdatedPayload,
            InteractionResolvedPayload,
        ),
    ):
        new_state, events = append_events(state, now, [event])
        return DispatchResult(new_state, events)

    # Unknown harness payload types are ignored at the dispatcher (adapter
    # should not emit them); treat as protocol error if they reach here.
    raise DomainError(
        ErrorCode.UNSUPPORTED_NATIVE_EVENT,
        f"unsupported harness event type: {type(event).__name__}",
    )


def mark_command_delivery_started(
    state: ConversationState,
    command_id: UUID,
    *,
    now: datetime,
) -> tuple[ConversationState, Command]:
    command = state.commands.get(command_id)
    if command is None:
        raise DomainError(ErrorCode.INVALID_STATE, "command not found in aggregate")
    updated = command.model_copy(
        update={
            "status": CommandStatus.DELIVERY_STARTED,
            "delivery_started_at": now,
        }
    )
    commands = dict(state.commands)
    commands[command_id] = updated
    return state.model_copy(update={"commands": commands}), updated


def mark_command_delivered(
    state: ConversationState,
    command_id: UUID,
    *,
    now: datetime,
) -> tuple[ConversationState, Command]:
    command = state.commands.get(command_id)
    if command is None:
        raise DomainError(ErrorCode.INVALID_STATE, "command not found in aggregate")
    updated = command.model_copy(
        update={
            "status": CommandStatus.DELIVERED,
            "delivered_at": now,
        }
    )
    commands = dict(state.commands)
    commands[command_id] = updated
    return state.model_copy(update={"commands": commands}), updated


def apply_outcome_unknown(
    state: ConversationState,
    *,
    now: datetime,
    delivery_phase: str | None = None,
    message: str | None = None,
) -> TransitionResult:
    return mark_outcome_unknown(
        state,
        now=now,
        delivery_phase=delivery_phase,
        message=message,
    )


def _settled_commands(
    new_state: ConversationState,
    old_state: ConversationState,
) -> tuple[Command, ...]:
    settled: list[Command] = []
    for command_id, command in new_state.commands.items():
        prev = old_state.commands.get(command_id)
        if prev is None:
            continue
        if command.status != prev.status and command.status in {
            CommandStatus.SETTLED,
            CommandStatus.OUTCOME_UNKNOWN,
        }:
            settled.append(command)
    return tuple(settled)

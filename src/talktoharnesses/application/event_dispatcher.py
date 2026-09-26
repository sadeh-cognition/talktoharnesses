"""Map normalized harness events onto pure domain transitions."""

from __future__ import annotations

from dataclasses import dataclass
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
    ProviderWarningPayload,
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
from talktoharnesses.domain.models import Command, PendingInteraction, SplitStreamCursor
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


@dataclass(frozen=True, slots=True)
class EventOrigin:
    """Where a harness event came from, committed together with the event.

    The native ids and stream offsets dedupe a replayed native history; the
    split frame lets a reattach replay only the split frames not committed.
    """

    native_ids: tuple[str, ...] = ()
    stream_offsets: tuple[str, ...] = ()
    split_stream: SplitStreamCursor | None = None


NO_ORIGIN = EventOrigin()


def dispatch_harness_event(
    state: ConversationState,
    event: HarnessEvent,
    *,
    now: datetime,
    origin: EventOrigin = NO_ORIGIN,
) -> DispatchResult:
    """Apply one harness event; returns updated state and durable envelopes."""
    state = _remember_origin(state, origin)

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
            ProviderWarningPayload,
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


def _remember_origin(state: ConversationState, origin: EventOrigin) -> ConversationState:
    if origin.split_stream is not None and origin.split_stream != state.split_stream:
        state = state.model_copy(update={"split_stream": origin.split_stream})
    if origin.native_ids or origin.stream_offsets:
        state = remember_native_ids(
            state,
            native_ids=origin.native_ids,
            stream_offsets=origin.stream_offsets,
        )
    return state


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

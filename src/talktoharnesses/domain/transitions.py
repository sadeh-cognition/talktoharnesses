"""Pure conversation state transitions (no I/O)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from talktoharnesses.domain._base import FROZEN, UtcDateTime, require_utc
from talktoharnesses.domain.enums import (
    ActivityStatus,
    ApprovalDecision,
    CommandKind,
    CommandStatus,
    ConversationStatus,
    ErrorCode,
    InteractionKind,
    InteractionStatus,
    TurnStatus,
)
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.domain.events import (
    ActivityCompletedPayload,
    ActivityStartedPayload,
    ConversationEvent,
    ConversationMetadataChangedPayload,
    ConversationTitleUpdatedPayload,
    EventPayload,
    HarnessSwitchedPayload,
    HarnessSwitchFailedPayload,
    InteractionDraftUpdatedPayload,
    InteractionRequestedPayload,
    InteractionResolvedPayload,
    RetentionExemptionChangedPayload,
    SessionClosedPayload,
    SessionFailedPayload,
    SessionReapedPayload,
    SessionResumedPayload,
    SessionRotatedPayload,
    SessionStartedPayload,
    TurnCancelledPayload,
    TurnCompletedPayload,
    TurnFailedPayload,
    TurnInterruptedPayload,
    TurnOutcomeUnknownPayload,
    TurnQueuedPayload,
    TurnStartedPayload,
    TurnSteeringPayload,
    TurnWaitingPayload,
)
from talktoharnesses.domain.models import (
    ApprovalRequestPayload,
    BackgroundActivity,
    Command,
    Conversation,
    ConversationHarnessBinding,
    EditQueuedPayload,
    HarnessCapabilities,
    HarnessConfiguration,
    InteractionAnswer,
    LaunchSnapshot,
    PendingInteraction,
    SteerPayload,
    StructuredQuestionPayload,
    SubmitTurnPayload,
    SwitchHarnessPayload,
    Turn,
)
from talktoharnesses.domain.questions import canonical_answer_values


class ConversationState(BaseModel):
    """In-memory aggregate used by pure transitions and tests."""

    model_config = FROZEN

    conversation: Conversation
    binding: ConversationHarnessBinding | None = None
    active_turn: Turn | None = None
    queued_turn: Turn | None = None
    queued_user_text: str | None = None
    commands: dict[UUID, Command] = Field(default_factory=lambda: {})
    interactions: dict[UUID, PendingInteraction] = Field(default_factory=lambda: {})
    answers: dict[UUID, InteractionAnswer] = Field(default_factory=lambda: {})
    activities: dict[UUID, BackgroundActivity] = Field(default_factory=lambda: {})
    capabilities: HarnessCapabilities | None = None
    idle_reap_eligible: bool = True
    # Native provider identity / stream-offset dedupe (Phase 4).
    seen_native_ids: frozenset[str] = Field(default_factory=frozenset)
    seen_stream_offsets: frozenset[str] = Field(default_factory=frozenset)


@dataclass(frozen=True, slots=True)
class TransitionResult:
    state: ConversationState
    events: tuple[ConversationEvent, ...]
    command: Command | None = None


def _now(now: datetime) -> datetime:
    return require_utc(now)


def _replace_conversation(state: ConversationState, **kwargs: Any) -> ConversationState:
    conversation = state.conversation.model_copy(update=kwargs)
    return state.model_copy(update={"conversation": conversation})


def _with_commands(state: ConversationState, commands: Mapping[UUID, Command]) -> ConversationState:
    return state.model_copy(update={"commands": dict(commands)})


def append_events(
    state: ConversationState,
    now: datetime,
    payloads: Sequence[EventPayload],
) -> tuple[ConversationState, tuple[ConversationEvent, ...]]:
    if not payloads:
        return state, ()
    ts = _now(now)
    sequence = state.conversation.next_event_sequence
    events: list[ConversationEvent] = []
    for payload in payloads:
        events.append(
            ConversationEvent(
                event_id=uuid4(),
                conversation_id=state.conversation.id,
                sequence=sequence,
                timestamp=ts,
                type=payload.type,
                payload=payload,
            )
        )
        sequence += 1
    new_state = _replace_conversation(
        state,
        next_event_sequence=sequence,
        updated_at=ts,
        version=state.conversation.version + 1,
    )
    return new_state, tuple(events)


def _running_activities(state: ConversationState) -> list[BackgroundActivity]:
    return [a for a in state.activities.values() if a.status == ActivityStatus.RUNNING]


def _recompute_status(state: ConversationState, now: datetime) -> ConversationState:
    if state.conversation.status == ConversationStatus.ARCHIVED:
        return state
    if state.active_turn is not None:
        if state.active_turn.status == TurnStatus.WAITING:
            status = ConversationStatus.WAITING
        else:
            status = ConversationStatus.RUNNING
        reap = False
    elif _running_activities(state):
        status = ConversationStatus.BACKGROUND_ACTIVE
        reap = False
    elif state.queued_turn is not None:
        status = ConversationStatus.IDLE
        reap = False
    else:
        status = ConversationStatus.IDLE
        reap = True
    return state.model_copy(
        update={
            "conversation": state.conversation.model_copy(
                update={"status": status, "updated_at": _now(now)}
            ),
            "idle_reap_eligible": reap,
        }
    )


def _find_command_by_idempotency(state: ConversationState, key: str) -> Command | None:
    for command in state.commands.values():
        if command.idempotency_key == key:
            return command
    return None


def _queue_prompt(
    state: ConversationState,
    *,
    prompt: str,
    command: Command,
    now: datetime,
    coalesce: bool,
) -> TransitionResult:
    ts = _now(now)
    if state.queued_turn is None:
        turn = Turn(
            id=uuid4(),
            conversation_id=state.conversation.id,
            status=TurnStatus.QUEUED,
            command_id=command.id,
            created_at=ts,
        )
        text = prompt
        coalesced = False
    else:
        turn = state.queued_turn
        existing = state.queued_user_text or ""
        text = f"{existing}\n{prompt}" if existing else prompt
        coalesced = True
        # Coalesce later commands into the executable queued command.
        if turn.command_id is not None and turn.command_id != command.id:
            command = command.model_copy(
                update={
                    "status": CommandStatus.COALESCED,
                    "coalesced_into_command_id": turn.command_id,
                    "target_turn_id": turn.id,
                }
            )
        else:
            command = command.model_copy(update={"target_turn_id": turn.id})

    command = command.model_copy(update={"target_turn_id": turn.id})
    commands = dict(state.commands)
    commands[command.id] = command
    new_state = state.model_copy(
        update={
            "queued_turn": turn,
            "queued_user_text": text,
            "commands": commands,
        }
    )
    executable_command_id = turn.command_id or command.id
    if command.status == CommandStatus.COALESCED:
        event_command_id = executable_command_id
    else:
        event_command_id = command.id
    payload = TurnQueuedPayload(
        turn_id=turn.id,
        command_id=event_command_id,
        prompt=text,
        coalesced=coalesced,
    )
    new_state, events = append_events(new_state, ts, [payload])
    new_state = _recompute_status(new_state, ts)
    return TransitionResult(state=new_state, events=events, command=command)


def submit_turn(
    state: ConversationState,
    *,
    prompt: str,
    idempotency_key: str,
    now: datetime,
    model: str | None = None,
    command_id: UUID | None = None,
) -> TransitionResult:
    """Accept a user prompt: start path via queue, or steer-or-queue when busy."""
    existing = _find_command_by_idempotency(state, idempotency_key)
    if existing is not None:
        return TransitionResult(state=state, events=(), command=existing)

    ts = _now(now)
    command = Command(
        id=command_id or uuid4(),
        conversation_id=state.conversation.id,
        kind=CommandKind.SUBMIT_TURN,
        status=CommandStatus.ACCEPTED,
        idempotency_key=idempotency_key,
        payload=SubmitTurnPayload(prompt=prompt, model=model),
        created_at=ts,
    )
    commands = dict(state.commands)
    commands[command.id] = command
    state = _with_commands(state, commands)

    if state.active_turn is not None and state.active_turn.status in {
        TurnStatus.RUNNING,
        TurnStatus.WAITING,
    }:
        # Steer-or-queue.
        supports_steer = bool(state.capabilities and state.capabilities.supports_steer)
        if supports_steer:
            return apply_steer(
                state,
                prompt=prompt,
                idempotency_key=idempotency_key,
                now=ts,
                command=command,
                steer_succeeded=True,
            )
        return _queue_prompt(state, prompt=prompt, command=command, now=ts, coalesce=True)

    # Background activity: still steer-or-queue for new prompts.
    if state.conversation.status == ConversationStatus.BACKGROUND_ACTIVE:
        supports_steer = bool(state.capabilities and state.capabilities.supports_steer)
        if supports_steer and state.active_turn is not None:
            return apply_steer(
                state,
                prompt=prompt,
                idempotency_key=idempotency_key,
                now=ts,
                command=command,
                steer_succeeded=True,
            )
        return _queue_prompt(state, prompt=prompt, command=command, now=ts, coalesce=True)

    return _queue_prompt(state, prompt=prompt, command=command, now=ts, coalesce=False)


def edit_queued_prompt(
    state: ConversationState,
    *,
    prompt: str,
    now: datetime,
) -> TransitionResult:
    if state.queued_turn is None or state.queued_user_text is None:
        raise DomainError(ErrorCode.NO_QUEUED_PROMPT, "no queued prompt to edit")
    if state.queued_turn.status != TurnStatus.QUEUED:
        raise DomainError(ErrorCode.QUEUED_PROMPT_NOT_EDITABLE, "queued prompt is not editable")

    turn = state.queued_turn
    new_state = state.model_copy(update={"queued_user_text": prompt})
    command_id = turn.command_id or uuid4()
    payload = TurnQueuedPayload(
        turn_id=turn.id,
        command_id=command_id,
        prompt=prompt,
        coalesced=False,
    )
    new_state, events = append_events(new_state, now, [payload])
    return TransitionResult(state=new_state, events=events)


def cancel_queued_prompt(state: ConversationState, *, now: datetime) -> TransitionResult:
    if state.queued_turn is None:
        raise DomainError(ErrorCode.NO_QUEUED_PROMPT, "no queued prompt to cancel")
    if state.queued_turn.status != TurnStatus.QUEUED:
        raise DomainError(ErrorCode.QUEUED_PROMPT_NOT_EDITABLE, "queued prompt cannot be cancelled")

    commands = dict(state.commands)
    if state.queued_turn.command_id and state.queued_turn.command_id in commands:
        cmd = commands[state.queued_turn.command_id]
        commands[cmd.id] = cmd.model_copy(
            update={"status": CommandStatus.SETTLED, "settled_at": _now(now)}
        )

    new_state = state.model_copy(
        update={
            "queued_turn": None,
            "queued_user_text": None,
            "commands": commands,
        }
    )
    new_state, events = append_events(
        new_state,
        now,
        [TurnCancelledPayload(turn_id=state.queued_turn.id)],
    )
    new_state = _recompute_status(new_state, now)
    return TransitionResult(state=new_state, events=events)


def start_turn(state: ConversationState, *, now: datetime) -> TransitionResult:
    """Promote the queued turn to the single active running turn."""
    if state.active_turn is not None and state.active_turn.status in {
        TurnStatus.RUNNING,
        TurnStatus.WAITING,
    }:
        raise DomainError(ErrorCode.CONVERSATION_BUSY, "conversation already has an active turn")
    if state.queued_turn is None:
        raise DomainError(ErrorCode.NO_QUEUED_PROMPT, "no queued turn to start")

    ts = _now(now)
    turn = state.queued_turn.model_copy(update={"status": TurnStatus.RUNNING, "started_at": ts})
    commands = dict(state.commands)
    if turn.command_id and turn.command_id in commands:
        cmd = commands[turn.command_id]
        commands[cmd.id] = cmd.model_copy(
            update={
                "status": CommandStatus.DELIVERY_STARTED,
                "delivery_started_at": ts,
                "target_turn_id": turn.id,
            }
        )

    new_state = state.model_copy(
        update={
            "active_turn": turn,
            "queued_turn": None,
            "queued_user_text": None,
            "commands": commands,
            "conversation": state.conversation.model_copy(
                update={
                    "active_turn_id": turn.id,
                    "status": ConversationStatus.RUNNING,
                    "updated_at": ts,
                }
            ),
            "idle_reap_eligible": False,
        }
    )
    new_state, events = append_events(
        new_state,
        ts,
        [TurnStartedPayload(turn_id=turn.id, command_id=turn.command_id)],
    )
    return TransitionResult(state=new_state, events=events)


def apply_steer(
    state: ConversationState,
    *,
    prompt: str,
    idempotency_key: str,
    now: datetime,
    command: Command | None = None,
    steer_succeeded: bool = True,
) -> TransitionResult:
    """Steer the active turn, or fall back to queue when unsupported/failed."""
    ts = _now(now)
    supports_steer = bool(state.capabilities and state.capabilities.supports_steer)
    active = state.active_turn

    if command is None:
        existing = _find_command_by_idempotency(state, idempotency_key)
        if existing is not None:
            return TransitionResult(state=state, events=(), command=existing)
        command = Command(
            id=uuid4(),
            conversation_id=state.conversation.id,
            kind=CommandKind.STEER,
            status=CommandStatus.ACCEPTED,
            idempotency_key=idempotency_key,
            payload=SteerPayload(prompt=prompt),
            created_at=ts,
            target_turn_id=active.id if active else None,
        )
        commands = dict(state.commands)
        commands[command.id] = command
        state = _with_commands(state, commands)

    if active is None or active.status not in {TurnStatus.RUNNING, TurnStatus.WAITING}:
        return _queue_prompt(state, prompt=prompt, command=command, now=ts, coalesce=True)

    if not supports_steer or not steer_succeeded:
        # Automatic queue fallback; keep command identity, retarget to queued turn.
        # Queued work is always submitted as a turn, even when it began as steer.
        command = command.model_copy(
            update={
                "kind": CommandKind.SUBMIT_TURN,
                "payload": SubmitTurnPayload(prompt=prompt),
            }
        )
        return _queue_prompt(state, prompt=prompt, command=command, now=ts, coalesce=True)

    command = command.model_copy(
        update={
            "kind": CommandKind.STEER,
            "target_turn_id": active.id,
            "payload": SteerPayload(prompt=prompt),
        }
    )
    commands = dict(state.commands)
    commands[command.id] = command
    new_state = _with_commands(state, commands)
    new_state, events = append_events(
        new_state,
        ts,
        [TurnSteeringPayload(turn_id=active.id, command_id=command.id, prompt=prompt)],
    )
    return TransitionResult(state=new_state, events=events, command=command)


def complete_turn(
    state: ConversationState,
    *,
    now: datetime,
    terminal_reason: str | None = None,
    has_assistant_message: bool = False,
) -> TransitionResult:
    """Terminal completion independent of a final assistant message."""
    if state.active_turn is None:
        raise DomainError(ErrorCode.NO_ACTIVE_TURN, "no active turn to complete")
    if state.active_turn.status not in {
        TurnStatus.RUNNING,
        TurnStatus.WAITING,
    }:
        raise DomainError(
            ErrorCode.INVALID_STATE,
            f"cannot complete turn in status {state.active_turn.status}",
        )

    cancelled = cancel_open_interactions(state, now=now)
    state = cancelled.state
    prefix = cancelled.events
    assert state.active_turn is not None

    ts = _now(now)
    turn = state.active_turn.model_copy(
        update={
            "status": TurnStatus.COMPLETED,
            "completed_at": ts,
            "terminal_reason": terminal_reason,
        }
    )
    commands = dict(state.commands)
    if turn.command_id and turn.command_id in commands:
        cmd = commands[turn.command_id]
        commands[cmd.id] = cmd.model_copy(
            update={"status": CommandStatus.SETTLED, "settled_at": ts}
        )

    new_state = state.model_copy(
        update={
            "active_turn": None,
            "commands": commands,
            "conversation": state.conversation.model_copy(
                update={"active_turn_id": None, "updated_at": ts}
            ),
        }
    )
    new_state, events = append_events(
        new_state,
        ts,
        [
            TurnCompletedPayload(
                turn_id=turn.id,
                terminal_reason=terminal_reason,
                has_assistant_message=has_assistant_message,
            )
        ],
    )
    new_state = _recompute_status(new_state, ts)
    return TransitionResult(state=new_state, events=prefix + events)


def register_activity(
    state: ConversationState,
    *,
    parent_turn_id: UUID,
    now: datetime,
    title: str | None = None,
    parent_activity_id: UUID | None = None,
    activity_id: UUID | None = None,
) -> TransitionResult:
    ts = _now(now)
    activity = BackgroundActivity(
        id=activity_id or uuid4(),
        conversation_id=state.conversation.id,
        parent_turn_id=parent_turn_id,
        parent_activity_id=parent_activity_id,
        status=ActivityStatus.RUNNING,
        title=title,
        created_at=ts,
    )
    activities = dict(state.activities)
    activities[activity.id] = activity
    new_state = state.model_copy(update={"activities": activities, "idle_reap_eligible": False})
    new_state, events = append_events(
        new_state,
        ts,
        [
            ActivityStartedPayload(
                activity_id=activity.id,
                parent_turn_id=parent_turn_id,
                parent_activity_id=parent_activity_id,
                title=title,
            )
        ],
    )
    new_state = _recompute_status(new_state, ts)
    return TransitionResult(state=new_state, events=events)


def complete_activity(
    state: ConversationState,
    *,
    activity_id: UUID,
    now: datetime,
    status: ActivityStatus = ActivityStatus.COMPLETED,
    summary: str | None = None,
) -> TransitionResult:
    if activity_id not in state.activities:
        raise DomainError(ErrorCode.INVALID_STATE, f"unknown activity {activity_id}")
    if status == ActivityStatus.RUNNING:
        raise DomainError(ErrorCode.INVALID_STATE, "activity completion status cannot be running")

    ts = _now(now)
    activity = state.activities[activity_id].model_copy(
        update={"status": status, "completed_at": ts, "summary": summary}
    )
    activities = dict(state.activities)
    activities[activity_id] = activity
    new_state = state.model_copy(update={"activities": activities})
    new_state, events = append_events(
        new_state,
        ts,
        [
            ActivityCompletedPayload(
                activity_id=activity_id,
                parent_turn_id=activity.parent_turn_id,
                status=status,
                summary=summary,
            )
        ],
    )
    new_state = _recompute_status(new_state, ts)
    return TransitionResult(state=new_state, events=events)


def fail_running_activities(
    state: ConversationState,
    *,
    now: datetime,
    summary: str = "worker_lost",
) -> TransitionResult:
    """Fail every running background activity with a fixed summary code."""
    current = state
    events: list[ConversationEvent] = []
    for activity in list(_running_activities(current)):
        result = complete_activity(
            current,
            activity_id=activity.id,
            now=now,
            status=ActivityStatus.FAILED,
            summary=summary,
        )
        current = result.state
        events.extend(result.events)
    return TransitionResult(state=current, events=tuple(events))


def _open_interactions(state: ConversationState) -> list[PendingInteraction]:
    return [
        i
        for i in state.interactions.values()
        if i.status in {InteractionStatus.PENDING, InteractionStatus.DRAFT}
    ]


def _request_bytes(interaction: PendingInteraction) -> bytes:
    return interaction.request.model_dump_json().encode("utf-8")


def _validate_interaction_answer(
    interaction: PendingInteraction,
    answer: InteractionAnswer,
) -> InteractionAnswer:
    if interaction.kind is InteractionKind.APPROVAL:
        if answer.answers is not None:
            raise DomainError(
                ErrorCode.INVALID_STATE,
                "approval interaction cannot include structured answers",
            )
        if answer.decision is None:
            raise DomainError(
                ErrorCode.INVALID_STATE,
                "approval interaction requires a decision",
            )
        if not isinstance(interaction.request, ApprovalRequestPayload):
            raise DomainError(ErrorCode.INVALID_STATE, "approval request payload mismatch")
        available = interaction.request.available_decisions
        if answer.decision not in available:
            raise DomainError(
                ErrorCode.INVALID_STATE,
                f"decision {answer.decision.value} not available for this interaction",
            )
        return answer
    if interaction.kind is InteractionKind.STRUCTURED_QUESTION:
        if answer.decision is not None:
            raise DomainError(
                ErrorCode.INVALID_STATE,
                "structured question cannot include an approval decision",
            )
        if answer.answers is None:
            raise DomainError(
                ErrorCode.INVALID_STATE,
                "structured question requires answers",
            )
        if not isinstance(interaction.request, StructuredQuestionPayload):
            raise DomainError(ErrorCode.INVALID_STATE, "structured question payload mismatch")
        return answer.model_copy(
            update={"answers": canonical_answer_values(answer, interaction.request.questions)}
        )
    raise DomainError(ErrorCode.INVALID_STATE, f"unknown interaction kind {interaction.kind}")


def _maybe_resume_after_interactions(
    state: ConversationState,
    *,
    now: datetime,
) -> ConversationState:
    """Return active turn to running only when no open interactions remain."""
    if state.active_turn is None or state.active_turn.status != TurnStatus.WAITING:
        return state
    if _open_interactions(state):
        return state
    ts = _now(now)
    return state.model_copy(
        update={
            "active_turn": state.active_turn.model_copy(update={"status": TurnStatus.RUNNING}),
            "conversation": state.conversation.model_copy(
                update={"status": ConversationStatus.RUNNING, "updated_at": ts}
            ),
        }
    )


def request_interaction(
    state: ConversationState,
    interaction: PendingInteraction,
    *,
    now: datetime,
) -> TransitionResult:
    """Accept a pending interaction. Multiple open interactions are allowed.

    Duplicate canonical ID with byte-identical request data is idempotent;
    conflicting reuse is ``invalid_state``.
    """
    if state.active_turn is None:
        raise DomainError(ErrorCode.NO_ACTIVE_TURN, "interaction requires an active turn")
    if interaction.conversation_id != state.conversation.id:
        raise DomainError(ErrorCode.INVALID_STATE, "interaction belongs to another conversation")
    if interaction.turn_id != state.active_turn.id:
        raise DomainError(ErrorCode.INVALID_STATE, "interaction belongs to another turn")

    existing = state.interactions.get(interaction.id)
    if existing is not None:
        if (
            existing.kind is interaction.kind
            and existing.turn_id == interaction.turn_id
            and _request_bytes(existing) == _request_bytes(interaction)
        ):
            return TransitionResult(state=state, events=())
        raise DomainError(
            ErrorCode.INVALID_STATE,
            f"interaction {interaction.id} already exists with different data",
        )

    pending = interaction.model_copy(update={"status": InteractionStatus.PENDING})
    interactions = dict(state.interactions)
    interactions[pending.id] = pending
    turn = state.active_turn.model_copy(update={"status": TurnStatus.WAITING})
    new_state = state.model_copy(
        update={
            "interactions": interactions,
            "active_turn": turn,
            "conversation": state.conversation.model_copy(
                update={"status": ConversationStatus.WAITING, "updated_at": _now(now)}
            ),
            "idle_reap_eligible": False,
        }
    )
    new_state, events = append_events(
        new_state,
        now,
        [
            InteractionRequestedPayload(
                turn_id=pending.turn_id,
                interaction_id=pending.id,
                kind=pending.kind,
                request=pending.request,
            ),
            TurnWaitingPayload(turn_id=turn.id, interaction_id=pending.id),
        ],
    )
    return TransitionResult(state=new_state, events=events)


def update_interaction_draft(
    state: ConversationState,
    *,
    interaction_id: UUID,
    draft: dict[str, Any],
    now: datetime,
) -> TransitionResult:
    if interaction_id not in state.interactions:
        raise DomainError(ErrorCode.INVALID_STATE, f"unknown interaction {interaction_id}")
    interaction = state.interactions[interaction_id]
    if interaction.status not in {InteractionStatus.PENDING, InteractionStatus.DRAFT}:
        raise DomainError(
            ErrorCode.INTERACTION_ALREADY_RESOLVED,
            "cannot edit a submitted, resolved, or cancelled interaction",
        )
    updated = interaction.model_copy(update={"status": InteractionStatus.DRAFT, "draft": draft})
    interactions = dict(state.interactions)
    interactions[interaction_id] = updated
    new_state = state.model_copy(update={"interactions": interactions})
    new_state, events = append_events(
        new_state,
        now,
        [InteractionDraftUpdatedPayload(interaction_id=interaction_id, draft=draft)],
    )
    return TransitionResult(state=new_state, events=events)


def submit_interaction_answer(
    state: ConversationState,
    answer: InteractionAnswer,
    *,
    now: datetime,
    automatic: bool = False,
) -> TransitionResult:
    """First-write-wins submission of an interaction answer.

    Does **not** create the ``answer_interaction`` command; the interaction
    broker releases that command only after the resolution event is published.
    """
    interaction_id = answer.interaction_id
    if interaction_id not in state.interactions:
        raise DomainError(ErrorCode.INVALID_STATE, f"unknown interaction {interaction_id}")

    if interaction_id in state.answers and not state.answers[interaction_id].is_draft:
        # First write wins: return existing without mutation.
        return TransitionResult(state=state, events=(), command=None)

    interaction = state.interactions[interaction_id]
    if interaction.status in {
        InteractionStatus.RESOLVED,
        InteractionStatus.SUBMITTED,
        InteractionStatus.CANCELLED,
    }:
        if interaction_id in state.answers and not state.answers[interaction_id].is_draft:
            return TransitionResult(state=state, events=(), command=None)
        raise DomainError(
            ErrorCode.INTERACTION_ALREADY_RESOLVED,
            "interaction already resolved",
        )

    answer = _validate_interaction_answer(interaction, answer)

    ts = _now(now)
    submitted = answer.model_copy(update={"is_draft": False, "submitted_at": ts})
    answers = dict(state.answers)
    answers[interaction_id] = submitted
    interactions = dict(state.interactions)
    interactions[interaction_id] = interaction.model_copy(
        update={"status": InteractionStatus.RESOLVED}
    )

    new_state = state.model_copy(update={"answers": answers, "interactions": interactions})
    new_state = _maybe_resume_after_interactions(new_state, now=ts)

    new_state, events = append_events(
        new_state,
        ts,
        [
            InteractionResolvedPayload(
                interaction_id=interaction_id,
                turn_id=interaction.turn_id,
                decision=submitted.decision,
                answers=submitted.answers,
                automatic=automatic,
            )
        ],
    )
    return TransitionResult(state=new_state, events=events)


def cancel_open_interactions(
    state: ConversationState,
    *,
    now: datetime,
) -> TransitionResult:
    """Cancel every still-open interaction and emit resolution events."""
    open_items = _open_interactions(state)
    if not open_items:
        return TransitionResult(state=state, events=())

    current = state
    events: list[ConversationEvent] = []
    for interaction in sorted(open_items, key=lambda i: (i.created_at, i.id)):
        result = cancel_interaction(current, interaction_id=interaction.id, now=now)
        current = result.state
        events.extend(result.events)
    return TransitionResult(state=current, events=tuple(events))


def cancel_interaction(
    state: ConversationState,
    *,
    interaction_id: UUID,
    now: datetime,
) -> TransitionResult:
    """Cancel one open interaction regardless of its request kind."""
    interaction = state.interactions.get(interaction_id)
    if interaction is None or interaction.status not in {
        InteractionStatus.PENDING,
        InteractionStatus.DRAFT,
    }:
        return TransitionResult(state=state, events=())
    ts = _now(now)
    interactions = dict(state.interactions)
    interactions[interaction_id] = interaction.model_copy(
        update={"status": InteractionStatus.CANCELLED}
    )
    answers = dict(state.answers)
    answers[interaction_id] = InteractionAnswer(
        interaction_id=interaction_id,
        decision=ApprovalDecision.CANCEL,
        is_draft=False,
        submitted_at=ts,
    )
    new_state = state.model_copy(update={"interactions": interactions, "answers": answers})
    new_state = _maybe_resume_after_interactions(new_state, now=ts)
    new_state, events = append_events(
        new_state,
        ts,
        [
            InteractionResolvedPayload(
                interaction_id=interaction_id,
                turn_id=interaction.turn_id,
                decision=ApprovalDecision.CANCEL,
                answers=None,
                automatic=False,
            )
        ],
    )
    return TransitionResult(state=new_state, events=events)


def _metadata_busy(state: ConversationState) -> bool:
    if state.active_turn is not None and state.active_turn.status in {
        TurnStatus.RUNNING,
        TurnStatus.WAITING,
        TurnStatus.QUEUED,
    }:
        return True
    if state.queued_turn is not None:
        return True
    return bool(_running_activities(state))


def _metadata_event(conversation: Conversation) -> ConversationMetadataChangedPayload:
    return ConversationMetadataChangedPayload(
        archived_at=conversation.archived_at,
        pinned_at=conversation.pinned_at,
        snoozed_until=conversation.snoozed_until,
        deleted_at=conversation.deleted_at,
    )


def _apply_metadata(
    state: ConversationState,
    *,
    now: datetime,
    **fields: Any,
) -> TransitionResult:
    ts = _now(now)
    updates = dict(fields)
    updates["updated_at"] = ts
    new_state = _replace_conversation(state, **updates)
    new_state, events = append_events(
        new_state,
        ts,
        [_metadata_event(new_state.conversation)],
    )
    return TransitionResult(state=new_state, events=events)


def archive_conversation(state: ConversationState, *, now: datetime) -> TransitionResult:
    if state.conversation.deleted_at is not None:
        raise DomainError(ErrorCode.NOT_FOUND, "conversation not found")
    if _metadata_busy(state):
        raise DomainError(ErrorCode.CONVERSATION_BUSY, "conversation is busy")
    ts = _now(now)
    return _apply_metadata(
        state,
        now=ts,
        archived_at=ts,
        status=ConversationStatus.ARCHIVED,
    )


def unarchive_conversation(state: ConversationState, *, now: datetime) -> TransitionResult:
    if state.conversation.deleted_at is not None:
        raise DomainError(ErrorCode.NOT_FOUND, "conversation not found")
    if state.conversation.status == ConversationStatus.ARCHIVED:
        # Restore execution status from turns/activities after clearing archive flag.
        new_state = _replace_conversation(state, archived_at=None, status=ConversationStatus.IDLE)
        new_state = _recompute_status(new_state, now)
    else:
        new_state = _replace_conversation(state, archived_at=None)
    ts = _now(now)
    new_state = _replace_conversation(new_state, updated_at=ts)
    new_state, events = append_events(
        new_state,
        ts,
        [_metadata_event(new_state.conversation)],
    )
    return TransitionResult(state=new_state, events=events)


def pin_conversation(state: ConversationState, *, now: datetime) -> TransitionResult:
    if state.conversation.deleted_at is not None:
        raise DomainError(ErrorCode.NOT_FOUND, "conversation not found")
    return _apply_metadata(state, now=now, pinned_at=_now(now))


def unpin_conversation(state: ConversationState, *, now: datetime) -> TransitionResult:
    if state.conversation.deleted_at is not None:
        raise DomainError(ErrorCode.NOT_FOUND, "conversation not found")
    return _apply_metadata(state, now=now, pinned_at=None)


def snooze_conversation(
    state: ConversationState,
    *,
    now: datetime,
    until: datetime,
) -> TransitionResult:
    if state.conversation.deleted_at is not None:
        raise DomainError(ErrorCode.NOT_FOUND, "conversation not found")
    return _apply_metadata(state, now=now, snoozed_until=_now(until))


def unsnooze_conversation(state: ConversationState, *, now: datetime) -> TransitionResult:
    if state.conversation.deleted_at is not None:
        raise DomainError(ErrorCode.NOT_FOUND, "conversation not found")
    return _apply_metadata(state, now=now, snoozed_until=None)


def soft_delete_conversation(state: ConversationState, *, now: datetime) -> TransitionResult:
    if state.conversation.deleted_at is not None:
        raise DomainError(ErrorCode.NOT_FOUND, "conversation not found")
    if _metadata_busy(state):
        raise DomainError(ErrorCode.CONVERSATION_BUSY, "conversation is busy")
    return _apply_metadata(state, now=now, deleted_at=_now(now))


def set_retention_exemption(
    state: ConversationState,
    *,
    now: datetime,
    exempt: bool,
) -> TransitionResult:
    """Set the per-conversation history retention exemption flag."""
    if state.conversation.deleted_at is not None:
        raise DomainError(ErrorCode.NOT_FOUND, "conversation not found")
    ts = _now(now)
    new_state = _replace_conversation(state, retention_exempt=exempt, updated_at=ts)
    new_state, events = append_events(
        new_state,
        ts,
        [RetentionExemptionChangedPayload(retention_exempt=exempt)],
    )
    return TransitionResult(state=new_state, events=events)


def interrupt_turn(
    state: ConversationState,
    *,
    now: datetime,
    reason: str | None = None,
) -> TransitionResult:
    if state.active_turn is None:
        raise DomainError(ErrorCode.NO_ACTIVE_TURN, "no active turn to interrupt")
    if state.active_turn.status not in {TurnStatus.RUNNING, TurnStatus.WAITING}:
        raise DomainError(
            ErrorCode.INVALID_STATE,
            f"cannot interrupt turn in status {state.active_turn.status}",
        )

    cancelled = cancel_open_interactions(state, now=now)
    state = cancelled.state
    prefix = cancelled.events
    assert state.active_turn is not None

    ts = _now(now)
    turn = state.active_turn.model_copy(
        update={
            "status": TurnStatus.INTERRUPTED,
            "completed_at": ts,
            "terminal_reason": reason or "interrupted",
        }
    )
    commands = dict(state.commands)
    if turn.command_id and turn.command_id in commands:
        cmd = commands[turn.command_id]
        commands[cmd.id] = cmd.model_copy(
            update={"status": CommandStatus.SETTLED, "settled_at": ts}
        )

    new_state = state.model_copy(
        update={
            "active_turn": None,
            "commands": commands,
            "conversation": state.conversation.model_copy(
                update={"active_turn_id": None, "updated_at": ts}
            ),
        }
    )
    new_state, events = append_events(
        new_state,
        ts,
        [TurnInterruptedPayload(turn_id=turn.id, reason=reason)],
    )
    new_state = _recompute_status(new_state, ts)
    return TransitionResult(state=new_state, events=prefix + events)


def fail_turn(
    state: ConversationState,
    *,
    now: datetime,
    error_code: str,
    message: str,
) -> TransitionResult:
    if state.active_turn is None:
        raise DomainError(ErrorCode.NO_ACTIVE_TURN, "no active turn to fail")

    cancelled = cancel_open_interactions(state, now=now)
    state = cancelled.state
    prefix = cancelled.events
    assert state.active_turn is not None

    ts = _now(now)
    turn = state.active_turn.model_copy(
        update={
            "status": TurnStatus.FAILED,
            "completed_at": ts,
            "terminal_reason": message,
        }
    )
    commands = dict(state.commands)
    if turn.command_id and turn.command_id in commands:
        cmd = commands[turn.command_id]
        commands[cmd.id] = cmd.model_copy(
            update={"status": CommandStatus.SETTLED, "settled_at": ts}
        )

    new_state = state.model_copy(
        update={
            "active_turn": None,
            "commands": commands,
            "conversation": state.conversation.model_copy(
                update={"active_turn_id": None, "updated_at": ts}
            ),
        }
    )
    new_state, events = append_events(
        new_state,
        ts,
        [TurnFailedPayload(turn_id=turn.id, error_code=error_code, message=message)],
    )
    new_state = _recompute_status(new_state, ts)
    return TransitionResult(state=new_state, events=prefix + events)


def mark_outcome_unknown(
    state: ConversationState,
    *,
    now: datetime,
    delivery_phase: str | None = None,
    message: str | None = None,
) -> TransitionResult:
    if state.active_turn is None:
        raise DomainError(ErrorCode.NO_ACTIVE_TURN, "no active turn for outcome_unknown")

    cancelled = cancel_open_interactions(state, now=now)
    state = cancelled.state
    prefix = cancelled.events
    assert state.active_turn is not None

    ts = _now(now)
    turn = state.active_turn.model_copy(
        update={
            "status": TurnStatus.OUTCOME_UNKNOWN,
            "completed_at": ts,
            "terminal_reason": message or "outcome_unknown",
        }
    )
    commands = dict(state.commands)
    if turn.command_id and turn.command_id in commands:
        cmd = commands[turn.command_id]
        commands[cmd.id] = cmd.model_copy(
            update={
                "status": CommandStatus.OUTCOME_UNKNOWN,
            }
        )

    new_state = state.model_copy(
        update={
            "active_turn": None,
            "commands": commands,
            "conversation": state.conversation.model_copy(
                update={"active_turn_id": None, "updated_at": ts}
            ),
        }
    )
    new_state, events = append_events(
        new_state,
        ts,
        [
            TurnOutcomeUnknownPayload(
                turn_id=turn.id,
                command_id=turn.command_id,
                delivery_phase=delivery_phase,
                message=message,
            )
        ],
    )
    new_state = _recompute_status(new_state, ts)
    return TransitionResult(state=new_state, events=prefix + events)


def change_mode(
    state: ConversationState,
    *,
    mode: str,
    now: datetime,
) -> TransitionResult:
    if state.active_turn is not None and state.active_turn.status in {
        TurnStatus.RUNNING,
        TurnStatus.WAITING,
    }:
        raise DomainError(
            ErrorCode.MODE_CHANGE_WHILE_ACTIVE,
            "cannot change mode while a turn is active",
        )
    if state.binding is None:
        raise DomainError(ErrorCode.INVALID_STATE, "no active binding to change mode")

    ts = _now(now)
    config = state.binding.configuration.model_copy(update={"mode": mode})
    binding = state.binding.model_copy(update={"configuration": config})
    new_state = state.model_copy(
        update={
            "binding": binding,
            "conversation": state.conversation.model_copy(update={"updated_at": ts}),
        }
    )
    return TransitionResult(state=new_state, events=())


def start_session(
    state: ConversationState,
    *,
    now: datetime,
    native_session_id: str | None,
    launch: LaunchSnapshot,
) -> TransitionResult:
    """Record a successful session start; update binding native ID and snapshot."""
    if state.binding is None:
        raise DomainError(ErrorCode.INVALID_STATE, "no binding for session start")
    ts = _now(now)
    binding = state.binding.model_copy(
        update={
            "native_session_id": native_session_id,
            "launch_snapshot": launch,
            "requires_session_recreation": False,
        }
    )
    new_state = state.model_copy(update={"binding": binding})
    new_state, events = append_events(
        new_state,
        ts,
        [
            SessionStartedPayload(
                binding_id=binding.id,
                native_session_id=native_session_id,
                harness_kind=binding.kind,
                model=launch.model,
                mode=launch.mode,
                effort=launch.effort,
            )
        ],
    )
    return TransitionResult(state=new_state, events=events)


def resume_session(
    state: ConversationState,
    *,
    now: datetime,
    native_session_id: str,
    launch: LaunchSnapshot,
) -> TransitionResult:
    """Record a successful session resume; update binding snapshot."""
    if state.binding is None:
        raise DomainError(ErrorCode.INVALID_STATE, "no binding for session resume")
    ts = _now(now)
    binding = state.binding.model_copy(
        update={
            "native_session_id": native_session_id,
            "launch_snapshot": launch,
            "requires_session_recreation": False,
        }
    )
    new_state = state.model_copy(update={"binding": binding})
    new_state, events = append_events(
        new_state,
        ts,
        [
            SessionResumedPayload(
                binding_id=binding.id,
                native_session_id=native_session_id,
                harness_kind=binding.kind,
            )
        ],
    )
    return TransitionResult(state=new_state, events=events)


def close_session(
    state: ConversationState,
    *,
    now: datetime,
    reason: str | None = None,
) -> TransitionResult:
    """Record a normal session close. Native resume ID is retained."""
    if state.binding is None:
        raise DomainError(ErrorCode.INVALID_STATE, "no binding to close")
    ts = _now(now)
    binding = state.binding
    new_state, events = append_events(
        state,
        ts,
        [SessionClosedPayload(binding_id=binding.id, reason=reason)],
    )
    return TransitionResult(state=new_state, events=events)


def fail_session(
    state: ConversationState,
    *,
    now: datetime,
    error_code: str,
    message: str,
) -> TransitionResult:
    """Record session startup or runtime failure."""
    if state.binding is None:
        raise DomainError(ErrorCode.INVALID_STATE, "no binding for session failure")
    ts = _now(now)
    binding = state.binding
    new_state, events = append_events(
        state,
        ts,
        [
            SessionFailedPayload(
                binding_id=binding.id,
                error_code=error_code,
                message=message,
            )
        ],
    )
    return TransitionResult(state=new_state, events=events)


def reap_session(
    state: ConversationState,
    *,
    now: datetime,
    reason: str | None = "idle",
) -> TransitionResult:
    if not state.idle_reap_eligible:
        raise DomainError(
            ErrorCode.INVALID_STATE,
            "session is not eligible for idle reaping",
        )
    if state.binding is None:
        raise DomainError(ErrorCode.INVALID_STATE, "no binding to reap")

    ts = _now(now)
    # Retain native resume identifier and every launch record.
    binding = state.binding
    new_state, events = append_events(
        state,
        ts,
        [SessionReapedPayload(binding_id=binding.id, reason=reason)],
    )
    return TransitionResult(state=new_state, events=events)


def rotate_session(
    state: ConversationState,
    *,
    now: datetime,
    reason: str | None = "retention",
) -> TransitionResult:
    if state.binding is None:
        raise DomainError(ErrorCode.INVALID_STATE, "no binding to rotate")
    if state.active_turn is not None:
        raise DomainError(ErrorCode.CONVERSATION_BUSY, "cannot rotate while turn is active")

    ts = _now(now)
    binding = state.binding.model_copy(
        update={
            "native_session_id": None,
            "requires_session_recreation": True,
        }
    )
    new_state = state.model_copy(
        update={
            "binding": binding,
            "seen_native_ids": frozenset(),
            "seen_stream_offsets": frozenset(),
        }
    )
    new_state, events = append_events(
        new_state,
        ts,
        [SessionRotatedPayload(binding_id=binding.id, reason=reason)],
    )
    return TransitionResult(state=new_state, events=events)


def mark_requires_recreation(
    state: ConversationState,
    *,
    now: datetime,
) -> TransitionResult:
    if state.binding is None:
        raise DomainError(ErrorCode.INVALID_STATE, "no binding to mark")
    ts = _now(now)
    binding = state.binding.model_copy(update={"requires_session_recreation": True})
    new_state = state.model_copy(
        update={
            "binding": binding,
            "conversation": state.conversation.model_copy(update={"updated_at": ts}),
        }
    )
    return TransitionResult(state=new_state, events=())


def commit_switch(
    state: ConversationState,
    *,
    new_binding: ConversationHarnessBinding,
    now: datetime,
) -> TransitionResult:
    if state.active_turn is not None:
        raise DomainError(ErrorCode.CONVERSATION_BUSY, "cannot switch while turn is active")
    if state.binding is None:
        raise DomainError(ErrorCode.INVALID_STATE, "no current binding to switch from")

    ts = _now(now)
    previous = state.binding.model_copy(update={"is_active": False, "closed_at": ts})
    active = new_binding.model_copy(update={"is_active": True})
    new_state = state.model_copy(
        update={
            "binding": active,
            "conversation": state.conversation.model_copy(
                update={
                    "current_binding_id": active.id,
                    "updated_at": ts,
                }
            ),
        }
    )
    new_state, events = append_events(
        new_state,
        ts,
        [
            HarnessSwitchedPayload(
                previous_binding_id=previous.id,
                new_binding_id=active.id,
                configuration=active.configuration,
            )
        ],
    )
    return TransitionResult(state=new_state, events=events)


def fail_switch(
    state: ConversationState,
    *,
    now: datetime,
    message: str,
    error_code: str | None = None,
) -> TransitionResult:
    if state.binding is None:
        raise DomainError(ErrorCode.INVALID_STATE, "no binding for switch failure")
    ts = _now(now)
    new_state, events = append_events(
        state,
        ts,
        [
            HarnessSwitchFailedPayload(
                binding_id=state.binding.id,
                message=message,
                error_code=error_code,
            )
        ],
    )
    return TransitionResult(state=new_state, events=events)


def apply_native_title(
    state: ConversationState,
    *,
    title_native: str,
    now: datetime,
) -> TransitionResult:
    """Update conversation.title_native and emit conversation_title_updated."""
    ts = _now(now)
    new_state = state.model_copy(
        update={
            "conversation": state.conversation.model_copy(
                update={"title_native": title_native, "updated_at": ts}
            )
        }
    )
    new_state, events = append_events(
        new_state,
        ts,
        [ConversationTitleUpdatedPayload(title_native=title_native)],
    )
    return TransitionResult(state=new_state, events=events)


def remember_native_ids(
    state: ConversationState,
    *,
    native_ids: Sequence[str] = (),
    stream_offsets: Sequence[str] = (),
) -> ConversationState:
    """Record native IDs / stream offsets for load-replay deduplication."""
    if not native_ids and not stream_offsets:
        return state
    return state.model_copy(
        update={
            "seen_native_ids": state.seen_native_ids | frozenset(native_ids),
            "seen_stream_offsets": state.seen_stream_offsets | frozenset(stream_offsets),
        }
    )


def new_conversation_state(
    *,
    owner_id: str,
    now: datetime,
    binding: ConversationHarnessBinding | None = None,
    capabilities: HarnessCapabilities | None = None,
    conversation_id: UUID | None = None,
) -> ConversationState:
    """Factory for an idle conversation aggregate."""
    ts = _now(now)
    conversation = Conversation(
        id=conversation_id or uuid4(),
        owner_id=owner_id,
        status=ConversationStatus.IDLE,
        created_at=ts,
        updated_at=ts,
        current_binding_id=binding.id if binding else None,
    )
    return ConversationState(
        conversation=conversation,
        binding=binding,
        capabilities=capabilities,
    )


# Re-export configuration type used by switch helpers for type checkers.
__all__ = [
    "ConversationState",
    "TransitionResult",
    "append_events",
    "apply_native_title",
    "apply_steer",
    "archive_conversation",
    "cancel_queued_prompt",
    "change_mode",
    "close_session",
    "commit_switch",
    "complete_activity",
    "complete_turn",
    "edit_queued_prompt",
    "fail_running_activities",
    "fail_session",
    "fail_switch",
    "fail_turn",
    "interrupt_turn",
    "mark_outcome_unknown",
    "mark_requires_recreation",
    "new_conversation_state",
    "pin_conversation",
    "reap_session",
    "register_activity",
    "remember_native_ids",
    "cancel_open_interactions",
    "request_interaction",
    "resume_session",
    "rotate_session",
    "set_retention_exemption",
    "snooze_conversation",
    "soft_delete_conversation",
    "start_session",
    "start_turn",
    "submit_interaction_answer",
    "submit_turn",
    "unarchive_conversation",
    "unpin_conversation",
    "unsnooze_conversation",
    "update_interaction_draft",
]

# Silence unused import warnings for types referenced in docs/signatures.
_ = (EditQueuedPayload, HarnessConfiguration, SwitchHarnessPayload, UtcDateTime)

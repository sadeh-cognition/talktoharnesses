"""Canonical conversation event envelope and typed payloads."""

from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, TypeAdapter, model_validator

from talktoharnesses.domain._base import FROZEN, UtcDateTime
from talktoharnesses.domain.enums import (
    ActivityStatus,
    ApprovalDecision,
    FileOperation,
    HarnessKind,
    InteractionKind,
    ToolOutcome,
    TurnStatus,
)
from talktoharnesses.domain.models import (
    HarnessConfiguration,
    InteractionRequestPayload,
    PlanItem,
)

# ---------------------------------------------------------------------------
# Session / process / harness
# ---------------------------------------------------------------------------


class SessionStartedPayload(BaseModel):
    model_config = FROZEN

    type: Literal["session_started"] = "session_started"
    binding_id: UUID
    native_session_id: str | None = None
    harness_kind: HarnessKind
    model: str | None = None
    mode: str | None = None


class SessionResumedPayload(BaseModel):
    model_config = FROZEN

    type: Literal["session_resumed"] = "session_resumed"
    binding_id: UUID
    native_session_id: str | None = None
    harness_kind: HarnessKind


class SessionReapedPayload(BaseModel):
    model_config = FROZEN

    type: Literal["session_reaped"] = "session_reaped"
    binding_id: UUID
    reason: str | None = None


class SessionRotatedPayload(BaseModel):
    model_config = FROZEN

    type: Literal["session_rotated"] = "session_rotated"
    binding_id: UUID
    reason: str | None = None


class SessionClosedPayload(BaseModel):
    model_config = FROZEN

    type: Literal["session_closed"] = "session_closed"
    binding_id: UUID
    reason: str | None = None


class SessionFailedPayload(BaseModel):
    model_config = FROZEN

    type: Literal["session_failed"] = "session_failed"
    binding_id: UUID | None = None
    error_code: str
    message: str


class ProcessStderrTruncatedPayload(BaseModel):
    model_config = FROZEN

    type: Literal["process_stderr_truncated"] = "process_stderr_truncated"
    process_id: UUID
    retained_bytes: int


class ProcessHealthPayload(BaseModel):
    model_config = FROZEN

    type: Literal["process_health"] = "process_health"
    process_id: UUID
    healthy: bool
    detail: str | None = None


class ProcessExitedPayload(BaseModel):
    model_config = FROZEN

    type: Literal["process_exited"] = "process_exited"
    process_id: UUID
    exit_code: int | None = None


class ProcessForcedTerminationPayload(BaseModel):
    model_config = FROZEN

    type: Literal["process_forced_termination"] = "process_forced_termination"
    process_id: UUID
    reason: str | None = None


class HarnessSwitchedPayload(BaseModel):
    model_config = FROZEN

    type: Literal["harness_switched"] = "harness_switched"
    previous_binding_id: UUID
    new_binding_id: UUID
    configuration: HarnessConfiguration


class HarnessSwitchFailedPayload(BaseModel):
    model_config = FROZEN

    type: Literal["harness_switch_failed"] = "harness_switch_failed"
    binding_id: UUID
    message: str
    error_code: str | None = None


# ---------------------------------------------------------------------------
# Turn lifecycle
# ---------------------------------------------------------------------------


class TurnQueuedPayload(BaseModel):
    model_config = FROZEN

    type: Literal["turn_queued"] = "turn_queued"
    turn_id: UUID
    command_id: UUID
    prompt: str
    coalesced: bool = False


class TurnStartedPayload(BaseModel):
    model_config = FROZEN

    type: Literal["turn_started"] = "turn_started"
    turn_id: UUID
    command_id: UUID | None = None


class TurnSteeringPayload(BaseModel):
    model_config = FROZEN

    type: Literal["turn_steering"] = "turn_steering"
    turn_id: UUID
    command_id: UUID
    prompt: str


class TurnWaitingPayload(BaseModel):
    model_config = FROZEN

    type: Literal["turn_waiting"] = "turn_waiting"
    turn_id: UUID
    interaction_id: UUID | None = None


class TurnCompletedPayload(BaseModel):
    model_config = FROZEN

    type: Literal["turn_completed"] = "turn_completed"
    turn_id: UUID
    terminal_reason: str | None = None
    has_assistant_message: bool = False


class TurnInterruptedPayload(BaseModel):
    model_config = FROZEN

    type: Literal["turn_interrupted"] = "turn_interrupted"
    turn_id: UUID
    reason: str | None = None


class TurnCancelledPayload(BaseModel):
    model_config = FROZEN

    type: Literal["turn_cancelled"] = "turn_cancelled"
    turn_id: UUID


class TurnFailedPayload(BaseModel):
    model_config = FROZEN

    type: Literal["turn_failed"] = "turn_failed"
    turn_id: UUID
    error_code: str
    message: str


class TurnOutcomeUnknownPayload(BaseModel):
    model_config = FROZEN

    type: Literal["turn_outcome_unknown"] = "turn_outcome_unknown"
    turn_id: UUID
    command_id: UUID | None = None
    delivery_phase: str | None = None
    message: str | None = None


# ---------------------------------------------------------------------------
# Messages / reasoning / plans
# ---------------------------------------------------------------------------


class AssistantMessageStartedPayload(BaseModel):
    model_config = FROZEN

    type: Literal["assistant_message_started"] = "assistant_message_started"
    turn_id: UUID
    message_id: UUID


class AssistantMessageDeltaPayload(BaseModel):
    model_config = FROZEN

    type: Literal["assistant_message_delta"] = "assistant_message_delta"
    turn_id: UUID
    message_id: UUID
    sequence: int
    text: str


class AssistantMessageCompletedPayload(BaseModel):
    model_config = FROZEN

    type: Literal["assistant_message_completed"] = "assistant_message_completed"
    turn_id: UUID
    message_id: UUID
    text: str


class ReasoningStartedPayload(BaseModel):
    model_config = FROZEN

    type: Literal["reasoning_started"] = "reasoning_started"
    turn_id: UUID
    reasoning_id: UUID


class ReasoningDeltaPayload(BaseModel):
    model_config = FROZEN

    type: Literal["reasoning_delta"] = "reasoning_delta"
    turn_id: UUID
    reasoning_id: UUID
    text: str


class ReasoningCompletedPayload(BaseModel):
    model_config = FROZEN

    type: Literal["reasoning_completed"] = "reasoning_completed"
    turn_id: UUID
    reasoning_id: UUID
    text: str


class PlanCreatedPayload(BaseModel):
    model_config = FROZEN

    type: Literal["plan_created"] = "plan_created"
    turn_id: UUID
    plan_id: UUID
    items: tuple[PlanItem, ...] = ()


class PlanUpdatedPayload(BaseModel):
    model_config = FROZEN

    type: Literal["plan_updated"] = "plan_updated"
    turn_id: UUID
    plan_id: UUID
    items: tuple[PlanItem, ...] = ()


# ---------------------------------------------------------------------------
# Tools / in-turn commands / files
# ---------------------------------------------------------------------------


class ToolRequestedPayload(BaseModel):
    model_config = FROZEN

    type: Literal["tool_requested"] = "tool_requested"
    turn_id: UUID
    tool_id: UUID
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolStartedPayload(BaseModel):
    model_config = FROZEN

    type: Literal["tool_started"] = "tool_started"
    turn_id: UUID
    tool_id: UUID
    tool_name: str


class ToolOutputDeltaPayload(BaseModel):
    model_config = FROZEN

    type: Literal["tool_output_delta"] = "tool_output_delta"
    turn_id: UUID
    tool_id: UUID
    sequence: int
    text: str


class ToolCompletedPayload(BaseModel):
    model_config = FROZEN

    type: Literal["tool_completed"] = "tool_completed"
    turn_id: UUID
    tool_id: UUID
    tool_name: str
    outcome: ToolOutcome
    exit_status: int | None = None
    output_tail: str = ""


class ToolFailedPayload(BaseModel):
    model_config = FROZEN

    type: Literal["tool_failed"] = "tool_failed"
    turn_id: UUID
    tool_id: UUID
    tool_name: str
    message: str


class CommandRequestedPayload(BaseModel):
    model_config = FROZEN

    type: Literal["command_requested"] = "command_requested"
    turn_id: UUID
    command_id: UUID
    argv: tuple[str, ...]


class CommandOutputPayload(BaseModel):
    model_config = FROZEN

    type: Literal["command_output"] = "command_output"
    turn_id: UUID
    command_id: UUID
    stream: Literal["stdout", "stderr"] = "stdout"
    text: str


class CommandExitedPayload(BaseModel):
    model_config = FROZEN

    type: Literal["command_exited"] = "command_exited"
    turn_id: UUID
    command_id: UUID
    exit_status: int


class FileChangeProposedPayload(BaseModel):
    model_config = FROZEN

    type: Literal["file_change_proposed"] = "file_change_proposed"
    turn_id: UUID
    file_change_id: UUID
    path: str
    operation: FileOperation
    patch: str | None = None


class FileChangeAppliedPayload(BaseModel):
    model_config = FROZEN

    type: Literal["file_change_applied"] = "file_change_applied"
    turn_id: UUID
    file_change_id: UUID
    path: str
    operation: FileOperation


# ---------------------------------------------------------------------------
# Interactions / activity / usage / provider
# ---------------------------------------------------------------------------


class InteractionRequestedPayload(BaseModel):
    model_config = FROZEN

    type: Literal["interaction_requested"] = "interaction_requested"
    turn_id: UUID
    interaction_id: UUID
    kind: InteractionKind
    request: InteractionRequestPayload


class InteractionDraftUpdatedPayload(BaseModel):
    model_config = FROZEN

    type: Literal["interaction_draft_updated"] = "interaction_draft_updated"
    interaction_id: UUID
    draft: dict[str, Any] = Field(default_factory=dict)


class InteractionResolvedPayload(BaseModel):
    model_config = FROZEN

    type: Literal["interaction_resolved"] = "interaction_resolved"
    interaction_id: UUID
    turn_id: UUID
    decision: ApprovalDecision | None = None
    answers: dict[str, Any] | None = None
    automatic: bool = False


class ActivityStartedPayload(BaseModel):
    model_config = FROZEN

    type: Literal["activity_started"] = "activity_started"
    activity_id: UUID
    parent_turn_id: UUID
    parent_activity_id: UUID | None = None
    title: str | None = None


class ActivityCompletedPayload(BaseModel):
    model_config = FROZEN

    type: Literal["activity_completed"] = "activity_completed"
    activity_id: UUID
    parent_turn_id: UUID
    status: ActivityStatus
    summary: str | None = None


class UsageUpdatedPayload(BaseModel):
    model_config = FROZEN

    type: Literal["usage_updated"] = "usage_updated"
    turn_id: UUID | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cached_input_tokens: int | None = None


class CostUpdatedPayload(BaseModel):
    model_config = FROZEN

    type: Literal["cost_updated"] = "cost_updated"
    turn_id: UUID | None = None
    cost: str  # decimal string for wire stability


class ProviderWarningPayload(BaseModel):
    model_config = FROZEN

    type: Literal["provider_warning"] = "provider_warning"
    message: str
    code: str | None = None


class ProviderIncompatiblePayload(BaseModel):
    model_config = FROZEN

    type: Literal["provider_incompatible"] = "provider_incompatible"
    harness_kind: HarnessKind
    observed_version: str | None = None
    supported_versions: tuple[str, ...] = ()
    message: str


class ProtocolErrorPayload(BaseModel):
    model_config = FROZEN

    type: Literal["protocol_error"] = "protocol_error"
    error_code: str
    message: str
    correlation_id: str | None = None


EventPayload = Annotated[
    SessionStartedPayload
    | SessionResumedPayload
    | SessionReapedPayload
    | SessionRotatedPayload
    | SessionClosedPayload
    | SessionFailedPayload
    | ProcessStderrTruncatedPayload
    | ProcessHealthPayload
    | ProcessExitedPayload
    | ProcessForcedTerminationPayload
    | HarnessSwitchedPayload
    | HarnessSwitchFailedPayload
    | TurnQueuedPayload
    | TurnStartedPayload
    | TurnSteeringPayload
    | TurnWaitingPayload
    | TurnCompletedPayload
    | TurnInterruptedPayload
    | TurnCancelledPayload
    | TurnFailedPayload
    | TurnOutcomeUnknownPayload
    | AssistantMessageStartedPayload
    | AssistantMessageDeltaPayload
    | AssistantMessageCompletedPayload
    | ReasoningStartedPayload
    | ReasoningDeltaPayload
    | ReasoningCompletedPayload
    | PlanCreatedPayload
    | PlanUpdatedPayload
    | ToolRequestedPayload
    | ToolStartedPayload
    | ToolOutputDeltaPayload
    | ToolCompletedPayload
    | ToolFailedPayload
    | CommandRequestedPayload
    | CommandOutputPayload
    | CommandExitedPayload
    | FileChangeProposedPayload
    | FileChangeAppliedPayload
    | InteractionRequestedPayload
    | InteractionDraftUpdatedPayload
    | InteractionResolvedPayload
    | ActivityStartedPayload
    | ActivityCompletedPayload
    | UsageUpdatedPayload
    | CostUpdatedPayload
    | ProviderWarningPayload
    | ProviderIncompatiblePayload
    | ProtocolErrorPayload,
    Field(discriminator="type"),
]


class ConversationEvent(BaseModel):
    """Durable conversation-local event envelope."""

    model_config = FROZEN

    event_id: UUID = Field(default_factory=uuid4)
    conversation_id: UUID
    sequence: int = Field(ge=1)
    timestamp: UtcDateTime
    type: str
    payload: EventPayload

    @model_validator(mode="after")
    def _type_matches_payload(self) -> ConversationEvent:
        if self.type != self.payload.type:
            msg = f"event type {self.type!r} does not match payload {self.payload.type!r}"
            raise ValueError(msg)
        return self


# Adapter-facing normalized stream items reuse the same payload vocabulary.
HarnessEvent = EventPayload

conversation_event_adapter: TypeAdapter[ConversationEvent] = TypeAdapter(ConversationEvent)
event_payload_adapter: TypeAdapter[EventPayload] = TypeAdapter(EventPayload)

# Keep TurnStatus import used for type completeness in callers/tests.
_ = TurnStatus

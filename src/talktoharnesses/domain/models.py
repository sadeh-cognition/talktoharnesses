"""Frozen domain entity, value, and API projection models."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, field_validator, model_validator

from talktoharnesses.domain._base import FROZEN, UtcDateTime
from talktoharnesses.domain.enums import (
    ActivityStatus,
    ApprovalDecision,
    CommandKind,
    CommandStatus,
    ConversationStatus,
    FileOperation,
    HarnessKind,
    InteractionKind,
    InteractionStatus,
    MessageRole,
    ProcessStatus,
    ToolOutcome,
    TurnStatus,
)

# ---------------------------------------------------------------------------
# Harness configuration / capabilities
# ---------------------------------------------------------------------------


class HarnessModelInfo(BaseModel):
    model_config = FROZEN

    id: str
    label: str | None = None


class HarnessModeInfo(BaseModel):
    model_config = FROZEN

    id: str
    label: str | None = None


class HarnessConfiguration(BaseModel):
    model_config = FROZEN

    kind: HarnessKind
    executable_path: str | None = None
    model: str | None = None
    mode: str | None = None
    working_directory: str
    workspace_roots: tuple[str, ...] = ()


class HarnessCapabilities(BaseModel):
    model_config = FROZEN

    kind: HarnessKind
    version: str
    supports_steer: bool = False
    supports_resume: bool = False
    supports_interrupt: bool = True
    supports_multi_interaction: bool = False
    supports_nested_activity: bool = False
    models: tuple[HarnessModelInfo, ...] = ()
    modes: tuple[HarnessModeInfo, ...] = ()


class HarnessInstance(BaseModel):
    model_config = FROZEN

    id: UUID = Field(default_factory=uuid4)
    owner_id: str
    name: str
    kind: HarnessKind
    configuration: HarnessConfiguration
    created_at: UtcDateTime


class LaunchSnapshot(BaseModel):
    model_config = FROZEN

    resolved_executable: str | None = None
    harness_version: str
    working_directory: str
    workspace_roots: tuple[str, ...] = ()
    model: str | None = None
    mode: str | None = None
    adapter_version: str
    capabilities: HarnessCapabilities


# ---------------------------------------------------------------------------
# Conversations and bindings
# ---------------------------------------------------------------------------


class Conversation(BaseModel):
    model_config = FROZEN

    id: UUID = Field(default_factory=uuid4)
    owner_id: str
    status: ConversationStatus = ConversationStatus.IDLE
    title_manual: str | None = None
    title_native: str | None = None
    title_derived: str | None = None
    pinned_at: UtcDateTime | None = None
    archived_at: UtcDateTime | None = None
    snoozed_until: UtcDateTime | None = None
    deleted_at: UtcDateTime | None = None
    version: int = 0
    next_event_sequence: int = 1
    active_turn_id: UUID | None = None
    current_binding_id: UUID | None = None
    created_at: UtcDateTime
    updated_at: UtcDateTime

    @property
    def display_title(self) -> str:
        if self.title_native:
            return self.title_native
        if self.title_manual:
            return self.title_manual
        if self.title_derived:
            return self.title_derived
        return "Untitled conversation"


class ConversationHarnessBinding(BaseModel):
    model_config = FROZEN

    id: UUID = Field(default_factory=uuid4)
    conversation_id: UUID
    kind: HarnessKind
    configuration: HarnessConfiguration
    native_session_id: str | None = None
    launch_snapshot: LaunchSnapshot | None = None
    requires_session_recreation: bool = False
    is_active: bool = True
    created_at: UtcDateTime
    closed_at: UtcDateTime | None = None


# ---------------------------------------------------------------------------
# Turns, messages, content
# ---------------------------------------------------------------------------


class Turn(BaseModel):
    model_config = FROZEN

    id: UUID = Field(default_factory=uuid4)
    conversation_id: UUID
    status: TurnStatus
    user_message_id: UUID | None = None
    command_id: UUID | None = None
    terminal_reason: str | None = None
    created_at: UtcDateTime
    started_at: UtcDateTime | None = None
    completed_at: UtcDateTime | None = None


class Message(BaseModel):
    model_config = FROZEN

    id: UUID = Field(default_factory=uuid4)
    turn_id: UUID
    role: MessageRole
    text: str = ""
    sequence: int = 0
    interrupted: bool = False
    created_at: UtcDateTime


class MessageChunk(BaseModel):
    model_config = FROZEN

    message_id: UUID
    sequence: int
    text: str


class ReasoningBlock(BaseModel):
    model_config = FROZEN

    id: UUID = Field(default_factory=uuid4)
    turn_id: UUID
    text: str = ""
    completed: bool = False


class PlanItem(BaseModel):
    model_config = FROZEN

    id: str
    title: str
    status: str | None = None
    detail: str | None = None


class Plan(BaseModel):
    model_config = FROZEN

    id: UUID = Field(default_factory=uuid4)
    turn_id: UUID
    items: tuple[PlanItem, ...] = ()


_MAX_TOOL_TAIL_BYTES = 2048


class CanonicalToolResult(BaseModel):
    model_config = FROZEN

    id: UUID = Field(default_factory=uuid4)
    turn_id: UUID
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    outcome: ToolOutcome = ToolOutcome.UNKNOWN
    exit_status: int | None = None
    paths: tuple[str, ...] = ()
    output_tail: str = ""
    full_output: str | None = None

    @field_validator("output_tail")
    @classmethod
    def _limit_tail(cls, value: str) -> str:
        encoded = value.encode("utf-8")
        if len(encoded) <= _MAX_TOOL_TAIL_BYTES:
            return value
        # Retain the newest output and repair a leading partial UTF-8 character.
        truncated = encoded[-_MAX_TOOL_TAIL_BYTES:]
        while truncated:
            try:
                return truncated.decode("utf-8")
            except UnicodeDecodeError:
                truncated = truncated[1:]
        return ""


class ToolOutputChunk(BaseModel):
    model_config = FROZEN

    tool_id: UUID
    sequence: int
    text: str


class FileChange(BaseModel):
    model_config = FROZEN

    id: UUID = Field(default_factory=uuid4)
    turn_id: UUID
    path: str
    operation: FileOperation
    patch: str | None = None
    summary: str | None = None


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


class SubmitTurnPayload(BaseModel):
    model_config = FROZEN

    kind: Literal["submit_turn"] = "submit_turn"
    prompt: str
    model: str | None = None


class SteerPayload(BaseModel):
    model_config = FROZEN

    kind: Literal["steer"] = "steer"
    prompt: str


class EditQueuedPayload(BaseModel):
    model_config = FROZEN

    kind: Literal["edit_queued"] = "edit_queued"
    prompt: str


class CancelQueuedPayload(BaseModel):
    model_config = FROZEN

    kind: Literal["cancel_queued"] = "cancel_queued"


class InterruptPayload(BaseModel):
    model_config = FROZEN

    kind: Literal["interrupt"] = "interrupt"


class AnswerInteractionPayload(BaseModel):
    model_config = FROZEN

    kind: Literal["answer_interaction"] = "answer_interaction"
    interaction_id: UUID


class SwitchHarnessPayload(BaseModel):
    model_config = FROZEN

    kind: Literal["switch_harness"] = "switch_harness"
    configuration: HarnessConfiguration


CommandPayload = Annotated[
    SubmitTurnPayload
    | SteerPayload
    | EditQueuedPayload
    | CancelQueuedPayload
    | InterruptPayload
    | AnswerInteractionPayload
    | SwitchHarnessPayload,
    Field(discriminator="kind"),
]


class Command(BaseModel):
    model_config = FROZEN

    id: UUID = Field(default_factory=uuid4)
    conversation_id: UUID
    kind: CommandKind
    status: CommandStatus = CommandStatus.ACCEPTED
    idempotency_key: str
    target_turn_id: UUID | None = None
    coalesced_into_command_id: UUID | None = None
    worker_id: str | None = None
    lease_expires_at: UtcDateTime | None = None
    attempts: int = 0
    delivery_started_at: UtcDateTime | None = None
    delivered_at: UtcDateTime | None = None
    settled_at: UtcDateTime | None = None
    native_session_id: str | None = None
    recovery_result: str | None = None
    payload: CommandPayload
    created_at: UtcDateTime

    @model_validator(mode="after")
    def _kind_matches_payload(self) -> Command:
        if self.kind.value != self.payload.kind:
            msg = f"command kind {self.kind.value!r} does not match payload {self.payload.kind!r}"
            raise ValueError(msg)
        return self


# ---------------------------------------------------------------------------
# Interactions and activities
# ---------------------------------------------------------------------------


class ApprovalRequestPayload(BaseModel):
    model_config = FROZEN

    kind: Literal["approval"] = "approval"
    tool_name: str | None = None
    command_args: tuple[str, ...] | None = None
    path: str | None = None
    operation: FileOperation | None = None
    summary: str | None = None


class StructuredQuestionPayload(BaseModel):
    model_config = FROZEN

    kind: Literal["structured_question"] = "structured_question"
    questions: tuple[dict[str, Any], ...] = ()


InteractionRequestPayload = Annotated[
    ApprovalRequestPayload | StructuredQuestionPayload,
    Field(discriminator="kind"),
]


class PendingInteraction(BaseModel):
    model_config = FROZEN

    id: UUID = Field(default_factory=uuid4)
    conversation_id: UUID
    turn_id: UUID
    kind: InteractionKind
    status: InteractionStatus = InteractionStatus.PENDING
    request: InteractionRequestPayload
    draft: dict[str, Any] | None = None
    created_at: UtcDateTime


class InteractionAnswer(BaseModel):
    model_config = FROZEN

    interaction_id: UUID
    decision: ApprovalDecision | None = None
    answers: dict[str, Any] | None = None
    is_draft: bool = False
    submitted_at: UtcDateTime | None = None


class BackgroundActivity(BaseModel):
    model_config = FROZEN

    id: UUID = Field(default_factory=uuid4)
    conversation_id: UUID
    parent_turn_id: UUID
    parent_activity_id: UUID | None = None
    status: ActivityStatus = ActivityStatus.RUNNING
    title: str | None = None
    summary: str | None = None
    created_at: UtcDateTime
    completed_at: UtcDateTime | None = None


# ---------------------------------------------------------------------------
# Process and usage
# ---------------------------------------------------------------------------


class ProcessRecord(BaseModel):
    model_config = FROZEN

    id: UUID = Field(default_factory=uuid4)
    conversation_id: UUID
    binding_id: UUID | None = None
    status: ProcessStatus
    pid: int | None = None
    started_at: UtcDateTime | None = None
    exited_at: UtcDateTime | None = None
    exit_code: int | None = None
    redacted_stderr_tail: str = ""


class UsageRecord(BaseModel):
    model_config = FROZEN

    turn_id: UUID | None = None
    conversation_id: UUID
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cached_input_tokens: int | None = None
    cost: Decimal | None = None


# ---------------------------------------------------------------------------
# API projections (shared Python / HTTP / SSE contracts)
# ---------------------------------------------------------------------------


class ConversationShell(BaseModel):
    model_config = FROZEN

    id: UUID
    title: str
    status: ConversationStatus
    harness_kind: HarnessKind | None = None
    model: str | None = None
    mode: str | None = None
    has_pending_interactions: bool = False
    pinned_at: UtcDateTime | None = None
    archived_at: UtcDateTime | None = None
    snoozed_until: UtcDateTime | None = None
    updated_at: UtcDateTime
    latest_activity_at: UtcDateTime | None = None


class TurnProjection(BaseModel):
    model_config = FROZEN

    id: UUID
    status: TurnStatus
    created_at: UtcDateTime
    started_at: UtcDateTime | None = None
    completed_at: UtcDateTime | None = None
    terminal_reason: str | None = None


class CommandProjection(BaseModel):
    model_config = FROZEN

    id: UUID
    kind: CommandKind
    status: CommandStatus
    target_turn_id: UUID | None = None
    idempotency_key: str
    created_at: UtcDateTime


class InteractionProjection(BaseModel):
    model_config = FROZEN

    id: UUID
    kind: InteractionKind
    status: InteractionStatus
    turn_id: UUID
    request: InteractionRequestPayload
    created_at: UtcDateTime


class ConversationDetail(BaseModel):
    model_config = FROZEN

    conversation: Conversation
    harness_kind: HarnessKind | None = None
    model: str | None = None
    mode: str | None = None
    turns: tuple[TurnProjection, ...] = ()
    pending_interactions: tuple[InteractionProjection, ...] = ()
    active_command: CommandProjection | None = None

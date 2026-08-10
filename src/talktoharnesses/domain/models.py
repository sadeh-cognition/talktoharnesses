"""Frozen domain entity, value, and API projection models."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Annotated, Any, Generic, Literal, TypeVar, cast
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, TypeAdapter, field_validator, model_validator

from talktoharnesses.domain._base import FROZEN, UtcDateTime
from talktoharnesses.domain.enums import (
    ActivityStatus,
    ApprovalDecision,
    ApprovalRuleDecision,
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
    retention_exempt: bool = False
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
    # Immutable source harness for harness_instance rule scope. None on legacy
    # bindings that cannot match harness-instance rules.
    harness_instance_id: UUID | None = None
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
    completed: bool = False
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


CANONICAL_TOOL_TAIL_BYTES = 2048


def limit_tool_output_tail(value: str) -> str:
    """Retain the newest canonical 2 KiB at a valid UTF-8 boundary."""
    encoded = value.encode("utf-8")
    if len(encoded) <= CANONICAL_TOOL_TAIL_BYTES:
        return value
    truncated = encoded[-CANONICAL_TOOL_TAIL_BYTES:]
    while truncated:
        try:
            return truncated.decode("utf-8")
        except UnicodeDecodeError:
            truncated = truncated[1:]
    return ""


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
        return limit_tool_output_tail(value)


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
    # Resolved target harness instance ID, when the switch targets an owned
    # HarnessRecord rather than an ad hoc configuration.
    harness_instance_id: UUID | None = None


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
    recovery_attempt_id: UUID | None = None
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


class CommandApprovalAction(BaseModel):
    model_config = FROZEN

    kind: Literal["command"] = "command"
    argv: tuple[str, ...] = Field(min_length=1)


class FileApprovalAction(BaseModel):
    model_config = FROZEN

    kind: Literal["file"] = "file"
    path: str = Field(min_length=1)
    operation: FileOperation


class NetworkApprovalAction(BaseModel):
    model_config = FROZEN

    kind: Literal["network"] = "network"


ApprovalAction = Annotated[
    CommandApprovalAction | FileApprovalAction | NetworkApprovalAction,
    Field(discriminator="kind"),
]


class ApprovalRequestPayload(BaseModel):
    model_config = FROZEN

    kind: Literal["approval"] = "approval"
    tool_name: str | None = None
    command_args: tuple[str, ...] | None = None
    path: str | None = None
    operation: FileOperation | None = None
    summary: str | None = None
    # Normalized action for automatic rule matching. Absent → manual-only.
    action: ApprovalAction | None = None
    available_decisions: tuple[ApprovalDecision, ...] = ()


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


# ---------------------------------------------------------------------------
# Persistent approval rules and audits
# ---------------------------------------------------------------------------


class ConversationRuleScope(BaseModel):
    model_config = FROZEN

    kind: Literal["conversation"] = "conversation"
    conversation_id: UUID


class HarnessInstanceRuleScope(BaseModel):
    model_config = FROZEN

    kind: Literal["harness_instance"] = "harness_instance"
    harness_instance_id: UUID


class ExecutableRuleScope(BaseModel):
    model_config = FROZEN

    kind: Literal["executable"] = "executable"
    resolved_executable: str = Field(min_length=1)


class UserRuleScope(BaseModel):
    model_config = FROZEN

    kind: Literal["user"] = "user"
    user_id: str = Field(min_length=1)


class PrincipalGlobalRuleScope(BaseModel):
    model_config = FROZEN

    kind: Literal["principal_global"] = "principal_global"


ApprovalRuleScope = Annotated[
    ConversationRuleScope
    | HarnessInstanceRuleScope
    | ExecutableRuleScope
    | UserRuleScope
    | PrincipalGlobalRuleScope,
    Field(discriminator="kind"),
]


class ExactArgvMatcher(BaseModel):
    model_config = FROZEN

    kind: Literal["exact_argv"] = "exact_argv"
    argv: tuple[str, ...] = Field(min_length=1)


class ExactPathMatcher(BaseModel):
    model_config = FROZEN

    kind: Literal["exact_path"] = "exact_path"
    path: str = Field(min_length=1)
    operation: FileOperation


class RecursiveDirectoryMatcher(BaseModel):
    model_config = FROZEN

    kind: Literal["recursive_directory"] = "recursive_directory"
    directory: str = Field(min_length=1)
    operation: FileOperation


class BlanketNetworkMatcher(BaseModel):
    model_config = FROZEN

    kind: Literal["blanket_network"] = "blanket_network"


ApprovalMatcher = Annotated[
    ExactArgvMatcher | ExactPathMatcher | RecursiveDirectoryMatcher | BlanketNetworkMatcher,
    Field(discriminator="kind"),
]


class ApprovalRuleInput(BaseModel):
    """Shared create/replace approval-rule body (no server-owned identity fields)."""

    model_config = FROZEN

    decision: ApprovalRuleDecision
    scope: ApprovalRuleScope
    matcher: ApprovalMatcher

    @field_validator("scope", mode="before")
    @classmethod
    def _scope(cls, value: object) -> ApprovalRuleScope:
        encoded = value.model_dump_json() if isinstance(value, BaseModel) else json.dumps(value)
        return cast(
            ApprovalRuleScope,
            TypeAdapter(ApprovalRuleScope).validate_json(encoded),
        )

    @field_validator("matcher", mode="before")
    @classmethod
    def _matcher(cls, value: object) -> ApprovalMatcher:
        encoded = value.model_dump_json() if isinstance(value, BaseModel) else json.dumps(value)
        return cast(
            ApprovalMatcher,
            TypeAdapter(ApprovalMatcher).validate_json(encoded),
        )


class ApprovalRule(BaseModel):
    model_config = FROZEN

    id: UUID = Field(default_factory=uuid4)
    principal_id: str
    decision: ApprovalRuleDecision
    scope: ApprovalRuleScope
    matcher: ApprovalMatcher
    created_at: UtcDateTime
    updated_at: UtcDateTime


class ApprovalRuleProjection(BaseModel):
    model_config = FROZEN

    id: UUID
    principal_id: str
    decision: ApprovalRuleDecision
    scope: ApprovalRuleScope
    matcher: ApprovalMatcher
    created_at: UtcDateTime
    updated_at: UtcDateTime


class InteractionAuditProjection(BaseModel):
    """Immutable outcome snapshot for approvals and structured questions."""

    model_config = FROZEN

    id: UUID
    principal_id: str
    interaction_id: UUID
    conversation_id: UUID
    turn_id: UUID
    kind: InteractionKind
    decision: ApprovalDecision | None = None
    answers: dict[str, Any] | None = None
    automatic: bool = False
    created_at: UtcDateTime
    provider_kind: HarnessKind | None = None
    provider_request_ids: dict[str, str] = Field(default_factory=dict)
    deciding_rule_id: UUID | None = None
    rule_decision: ApprovalRuleDecision | None = None
    rule_scope: ApprovalRuleScope | None = None
    rule_matcher: ApprovalMatcher | None = None
    request_action: ApprovalAction | None = None


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
    orphaned_at: UtcDateTime | None = None
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


T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    """Keyset page shared by facade reads, HTTP JSON, and list endpoints."""

    model_config = FROZEN

    items: tuple[T, ...] = ()
    next_cursor: str | None = None


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


class SearchSnippet(BaseModel):
    model_config = FROZEN

    text: str
    matched_terms: tuple[str, ...] = ()


class ConversationSearchHit(BaseModel):
    model_config = FROZEN

    conversation: ConversationShell
    snippet: SearchSnippet | None = None


class RetentionPolicyProjection(BaseModel):
    model_config = FROZEN

    months: int = Field(ge=1, le=120)
    updated_at: UtcDateTime | None = None


class RetentionPreviewProjection(BaseModel):
    model_config = FROZEN

    cutoff: UtcDateTime
    soft_deleted_conversations: int
    history_conversations: int
    terminal_turns: int
    waiting_turns: int


class HarnessProjection(BaseModel):
    model_config = FROZEN

    id: UUID
    owner_id: str
    name: str
    kind: HarnessKind
    configuration: HarnessConfiguration
    created_at: UtcDateTime


class HarnessProbeProjection(BaseModel):
    model_config = FROZEN

    harness_id: UUID
    capabilities: HarnessCapabilities
    probed_at: UtcDateTime


class TurnProjection(BaseModel):
    model_config = FROZEN

    id: UUID
    conversation_id: UUID | None = None
    status: TurnStatus
    user_message_id: UUID | None = None
    command_id: UUID | None = None
    created_at: UtcDateTime
    started_at: UtcDateTime | None = None
    completed_at: UtcDateTime | None = None
    terminal_reason: str | None = None


class MessageProjection(BaseModel):
    model_config = FROZEN

    id: UUID
    turn_id: UUID
    role: MessageRole
    text: str = ""
    sequence: int = 0
    interrupted: bool = False
    created_at: UtcDateTime


class ToolProjection(BaseModel):
    model_config = FROZEN

    id: UUID
    turn_id: UUID
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    outcome: ToolOutcome = ToolOutcome.UNKNOWN
    exit_status: int | None = None
    paths: tuple[str, ...] = ()
    output_tail: str = ""


class PlanProjection(BaseModel):
    model_config = FROZEN

    id: UUID
    turn_id: UUID
    items: tuple[PlanItem, ...] = ()


class ActivityProjection(BaseModel):
    model_config = FROZEN

    id: UUID
    conversation_id: UUID
    parent_turn_id: UUID
    parent_activity_id: UUID | None = None
    status: ActivityStatus = ActivityStatus.RUNNING
    title: str | None = None
    summary: str | None = None
    created_at: UtcDateTime
    completed_at: UtcDateTime | None = None


class CommandProjection(BaseModel):
    model_config = FROZEN

    id: UUID
    kind: CommandKind
    status: CommandStatus
    target_turn_id: UUID | None = None
    idempotency_key: str
    created_at: UtcDateTime


class InteractionResolutionResult(BaseModel):
    """First-write-wins resolution outcome returned to facade/broker callers."""

    model_config = FROZEN

    answer: InteractionAnswer
    command: CommandProjection | None = None
    was_first_write: bool
    audit: InteractionAuditProjection | None = None


class InteractionProjection(BaseModel):
    model_config = FROZEN

    id: UUID
    kind: InteractionKind
    status: InteractionStatus
    turn_id: UUID
    request: InteractionRequestPayload
    draft: dict[str, Any] | None = None
    created_at: UtcDateTime


class ConversationDetail(BaseModel):
    model_config = FROZEN

    conversation: Conversation
    harness_kind: HarnessKind | None = None
    model: str | None = None
    mode: str | None = None
    turns: tuple[TurnProjection, ...] = ()
    messages: tuple[MessageProjection, ...] = ()
    tools: tuple[ToolProjection, ...] = ()
    plans: tuple[PlanProjection, ...] = ()
    activity: tuple[ActivityProjection, ...] = ()
    pending_interactions: tuple[InteractionProjection, ...] = ()
    active_command: CommandProjection | None = None


class ConversationSnapshot(BaseModel):
    model_config = FROZEN

    sequence: int = Field(ge=0)
    detail: ConversationDetail


class SyncProjection(BaseModel):
    model_config = FROZEN

    sequence: int = Field(ge=0)


class SubmitTurnResult(BaseModel):
    model_config = FROZEN

    command: CommandProjection
    turn: TurnProjection


class TokenProjection(BaseModel):
    """Bearer token response. Never includes raw jti."""

    model_config = FROZEN

    token: str
    expires_at: UtcDateTime


class ErrorProjection(BaseModel):
    model_config = FROZEN

    code: str
    message: str


class ReadinessProjection(BaseModel):
    model_config = FROZEN

    ready: bool
    reason: Literal["ready", "not_ready"]

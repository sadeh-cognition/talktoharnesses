"""Harness-facing wire models shared by the proxy and split services."""

from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from tth_types.base import FROZEN, UtcDateTime
from tth_types.enums import ApprovalDecision, FileOperation, HarnessKind, ToolOutcome

# ---------------------------------------------------------------------------
# Harness configuration / capabilities
# ---------------------------------------------------------------------------


class HarnessEffortInfo(BaseModel):
    model_config = FROZEN

    id: str
    label: str | None = None


class HarnessModelInfo(BaseModel):
    model_config = FROZEN

    id: str
    label: str | None = None
    # None inherits provider-wide efforts; an empty tuple means unsupported.
    efforts: tuple[HarnessEffortInfo, ...] | None = None


class HarnessModeInfo(BaseModel):
    model_config = FROZEN

    id: str
    label: str | None = None


class HarnessConfiguration(BaseModel):
    model_config = FROZEN

    kind: HarnessKind
    model: str | None = None
    mode: str | None = None
    effort: str | None = None
    yolo: bool = False
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
    efforts: tuple[HarnessEffortInfo, ...] = ()


class VersionAdvisory(BaseModel):
    """How the probed identity compares to the packaged floor and last live proof."""

    model_config = FROZEN

    status: Literal["verified", "behind_verified", "ahead_of_verified", "unknown"]
    probed_version: str
    floor_version: str
    latest_verified: str | None = None


class LaunchSnapshot(BaseModel):
    model_config = FROZEN

    resolved_executable: str | None = None
    harness_version: str
    working_directory: str
    workspace_roots: tuple[str, ...] = ()
    model: str | None = None
    mode: str | None = None
    effort: str | None = None
    adapter_version: str
    capabilities: HarnessCapabilities


# ---------------------------------------------------------------------------
# Plans and tool output limits
# ---------------------------------------------------------------------------


class PlanItem(BaseModel):
    model_config = FROZEN

    id: str
    title: str
    status: str | None = None
    detail: str | None = None


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


# ---------------------------------------------------------------------------
# Interaction request/answer payloads
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


class CanonicalQuestionOption(BaseModel):
    model_config = FROZEN

    label: str = Field(min_length=1)
    value: str = Field(min_length=1)
    description: str | None = None


class CanonicalQuestion(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
        populate_by_name=True,
        serialize_by_alias=True,
    )

    id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    options: tuple[CanonicalQuestionOption, ...] = ()
    multi_select: bool = Field(default=False, alias="multiSelect")
    header: str | None = None
    allow_other: bool = Field(default=False, alias="allowOther")
    is_secret: bool = Field(default=False, alias="isSecret")


class StructuredQuestionPayload(BaseModel):
    model_config = FROZEN

    kind: Literal["structured_question"] = "structured_question"
    questions: tuple[CanonicalQuestion, ...] = Field(min_length=1)


InteractionRequestPayload = Annotated[
    ApprovalRequestPayload | StructuredQuestionPayload,
    Field(discriminator="kind"),
]


class InteractionAnswer(BaseModel):
    model_config = FROZEN

    interaction_id: UUID
    decision: ApprovalDecision | None = None
    answers: dict[str, Any] | None = None
    is_draft: bool = False
    submitted_at: UtcDateTime | None = None

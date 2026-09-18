"""Strict adapter-owned Codex notification and approval schemas."""

from __future__ import annotations

import re
import shlex
from abc import ABC, abstractmethod
from typing import Any, ClassVar, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from tth_types.enums import ApprovalDecision, FileOperation, InteractionKind
from tth_types.harness import (
    ApprovalRequestPayload,
    CanonicalQuestion,
    CommandApprovalAction,
    FileApprovalAction,
    InteractionAnswer,
    InteractionRequestPayload,
    StructuredQuestionPayload,
)

from tth_codex.shared.questions import canonical_answer_values, canonical_questions

_STRICT = ConfigDict(extra="forbid", frozen=True)
_STRICT_ALIASED = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class CodexTurnUsage(BaseModel):
    model_config = _STRICT

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cached_input_tokens: int | None = None


class CodexAgentMessageDelta(BaseModel):
    model_config = _STRICT

    method: Literal["agentMessageDelta"] = "agentMessageDelta"
    thread_id: str
    turn_id: str
    item_id: str
    delta: str


class CodexReasoningDelta(BaseModel):
    model_config = _STRICT

    method: Literal["reasoningDelta"] = "reasoningDelta"
    thread_id: str
    turn_id: str
    item_id: str
    delta: str


class CodexTurnStarted(BaseModel):
    model_config = _STRICT

    method: Literal["turnStarted"] = "turnStarted"
    thread_id: str
    turn_id: str


class CodexError(BaseModel):
    model_config = _STRICT

    method: Literal["error"] = "error"
    thread_id: str
    turn_id: str
    message: str
    will_retry: bool


class CodexTurnCompleted(BaseModel):
    model_config = _STRICT

    method: Literal["turnCompleted"] = "turnCompleted"
    thread_id: str
    turn_id: str
    status: str
    final_response: str | None = None
    error_message: str | None = None


class CodexTokenUsageUpdated(BaseModel):
    model_config = _STRICT

    method: Literal["tokenUsageUpdated"] = "tokenUsageUpdated"
    thread_id: str
    turn_id: str
    # What the thread's last request spent.
    usage: CodexTurnUsage
    # What the thread has spent since it opened, which the turn's own total is
    # measured against. Absent on hosts that report only ``usage``.
    thread_total: CodexTurnUsage | None = None


class CodexItemStarted(BaseModel):
    model_config = _STRICT

    method: Literal["itemStarted"] = "itemStarted"
    thread_id: str
    turn_id: str
    item_id: str
    item_type: str
    title: str | None = None
    # The shell command line of a commandExecution item. Kept apart from
    # ``title`` so the tool name stays a name rather than a whole command.
    command: str | None = None


class CodexItemCompleted(BaseModel):
    model_config = _STRICT

    method: Literal["itemCompleted"] = "itemCompleted"
    thread_id: str
    turn_id: str
    item_id: str
    item_type: str
    status: str | None = None


class CodexExecpolicyAmendment(BaseModel):
    model_config = _STRICT_ALIASED

    prefix: list[str] | None = None


_DECISION_NAMES = {
    ApprovalDecision.ALLOW_ONCE: "accept",
    ApprovalDecision.ALLOW_SESSION: "acceptForSession",
    ApprovalDecision.DENY: "decline",
    ApprovalDecision.CANCEL: "cancel",
}


def mcp_tool_name(server: str, tool: str) -> str:
    """The ``mcp__<server>__<tool>`` name every adapter reports for MCP tools."""
    return f"mcp__{server}__{tool}"


def _file_operation(kind: str | None) -> FileOperation:
    normalized = (kind or "").lower()
    if normalized in {"create", "add", "write"}:
        return FileOperation.CREATE
    if normalized in {"delete", "remove", "unlink"}:
        return FileOperation.DELETE
    if normalized in {"read", "view"}:
        return FileOperation.READ
    return FileOperation.MODIFY


class CodexServerRequest(BaseModel, ABC):
    """A blocking Codex server request brokered as one canonical interaction.

    Each kind translates both ways: its native params into the canonical
    request, and the canonical answer back into Codex's native result.
    """

    interaction_kind: ClassVar[InteractionKind]

    @abstractmethod
    def to_request(self) -> InteractionRequestPayload: ...

    @abstractmethod
    def to_native_result(self, answer: InteractionAnswer) -> dict[str, Any]: ...


class CodexApproval(CodexServerRequest):
    """An approval request: the decisions it offers and how its answer is encoded.

    A decision the request never offered fails closed to a decline.
    """

    interaction_kind: ClassVar[InteractionKind] = InteractionKind.APPROVAL
    offered_decisions: ClassVar[tuple[ApprovalDecision, ...]]

    def to_native_result(self, answer: InteractionAnswer) -> dict[str, Any]:
        decision = answer.decision
        if decision is None or decision not in self.offered_decisions:
            decision = ApprovalDecision.DENY
        return self._encode(_DECISION_NAMES[decision])

    def _encode(self, name: str) -> dict[str, Any]:
        return {"decision": name}


class CodexCommandApprovalParams(CodexApproval):
    """Typed params for item/commandExecution/requestApproval."""

    model_config = _STRICT_ALIASED

    offered_decisions: ClassVar[tuple[ApprovalDecision, ...]] = (
        ApprovalDecision.ALLOW_ONCE,
        ApprovalDecision.ALLOW_SESSION,
        ApprovalDecision.DENY,
        ApprovalDecision.CANCEL,
    )

    kind: Literal["command"] = "command"
    thread_id: str | None = Field(default=None, alias="threadId")
    turn_id: str | None = Field(default=None, alias="turnId")
    item_id: str | None = Field(default=None, alias="itemId")
    command: list[str] | None = None
    cwd: str | None = None
    reason: str | None = None
    risk: Any | None = None
    parsed_cmd: list[Any] | None = Field(default=None, alias="parsedCmd")
    proposed_execpolicy_amendment: CodexExecpolicyAmendment | None = Field(
        default=None,
        alias="proposedExecpolicyAmendment",
    )
    started_at_ms: int | None = Field(default=None, alias="startedAtMs")
    environment_id: str | None = Field(default=None, alias="environmentId")
    command_actions: list[Any] | None = Field(default=None, alias="commandActions")
    available_decisions: list[Any] | None = Field(default=None, alias="availableDecisions")

    @field_validator("command", mode="before")
    @classmethod
    def _coerce_command(cls, value: object) -> object:
        if isinstance(value, str):
            return shlex.split(value)
        return value

    @field_validator("proposed_execpolicy_amendment", mode="before")
    @classmethod
    def _coerce_amendment(cls, value: object) -> object:
        if isinstance(value, list):
            return {"prefix": [str(item) for item in cast(list[object], value)]}
        return value

    def to_request(self) -> ApprovalRequestPayload:
        argv = tuple(self.command or ())
        return ApprovalRequestPayload(
            tool_name="commandExecution",
            command_args=argv or None,
            summary=self.reason or "Codex command approval",
            action=CommandApprovalAction(argv=argv) if argv else None,
            available_decisions=self.offered_decisions,
        )


class CodexFileChangeEntry(BaseModel):
    model_config = _STRICT_ALIASED

    path: str
    kind: str | None = None


class CodexFileApprovalParams(CodexApproval):
    """Typed params for item/fileChange/requestApproval."""

    model_config = _STRICT_ALIASED

    offered_decisions: ClassVar[tuple[ApprovalDecision, ...]] = (
        ApprovalDecision.ALLOW_ONCE,
        ApprovalDecision.DENY,
        ApprovalDecision.CANCEL,
    )

    thread_id: str | None = Field(default=None, alias="threadId")
    turn_id: str | None = Field(default=None, alias="turnId")
    item_id: str | None = Field(default=None, alias="itemId")
    files: list[CodexFileChangeEntry] | None = None
    reason: str | None = None

    def to_request(self) -> ApprovalRequestPayload:
        first = self.files[0] if self.files else None
        path = first.path if first is not None else None
        operation = _file_operation(first.kind if first is not None else None)
        return ApprovalRequestPayload(
            tool_name="fileChange",
            path=path,
            operation=operation,
            summary=self.reason or "Codex file change approval",
            action=FileApprovalAction(path=path, operation=operation) if path else None,
            available_decisions=self.offered_decisions,
        )


class CodexMcpToolApprovalMetadata(BaseModel):
    # Codex also sends display details and persistence options. Neither grants
    # permission: this adapter offers only a decision for the current call.
    model_config = ConfigDict(extra="allow", frozen=True)

    codex_approval_kind: Literal["mcp_tool_call"]


class CodexMcpToolApprovalParams(CodexApproval):
    """Codex's confirmation-only MCP tool elicitation, not a server data form."""

    model_config = _STRICT_ALIASED

    offered_decisions: ClassVar[tuple[ApprovalDecision, ...]] = (
        ApprovalDecision.ALLOW_ONCE,
        ApprovalDecision.DENY,
        ApprovalDecision.CANCEL,
    )

    thread_id: str = Field(alias="threadId")
    turn_id: str | None = Field(default=None, alias="turnId")
    server_name: str = Field(alias="serverName", min_length=1)
    mode: Literal["form"]
    metadata: CodexMcpToolApprovalMetadata = Field(alias="_meta")
    message: str
    requested_schema: dict[str, Any] = Field(alias="requestedSchema")
    tool_name: str
    """``mcp__<server>__<tool>``, read from the confirmation message."""

    @model_validator(mode="before")
    @classmethod
    def _read_tool_name(cls, data: object) -> object:
        # Codex 0.154 names the tool only in this native confirmation message,
        # not in _meta. Require its exact format and server identity.
        if not isinstance(data, dict):
            return data
        raw = cast(dict[str, object], data)
        server, message = raw.get("serverName"), raw.get("message")
        if not (isinstance(server, str) and isinstance(message, str)):
            return raw
        match = re.fullmatch(
            rf'Allow the {re.escape(server)} MCP server to run tool "([^"\n]+)"\?',
            message,
        )
        if match is None:
            raise ValueError("unrecognized Codex MCP tool approval message")
        return {**raw, "tool_name": mcp_tool_name(server, match[1])}

    @model_validator(mode="after")
    def _confirmation_only(self) -> CodexMcpToolApprovalParams:
        if self.requested_schema != {"type": "object", "properties": {}}:
            raise ValueError("MCP tool approval must not request form data")
        return self

    def to_request(self) -> ApprovalRequestPayload:
        return ApprovalRequestPayload(
            tool_name=self.tool_name,
            summary=self.message,
            available_decisions=self.offered_decisions,
        )

    def _encode(self, name: str) -> dict[str, Any]:
        return {"action": name, "content": {} if name == "accept" else None}


CodexApprovalParams = (
    CodexCommandApprovalParams | CodexFileApprovalParams | CodexMcpToolApprovalParams
)


class CodexUserInputOption(BaseModel):
    model_config = _STRICT_ALIASED

    label: str
    description: str


class CodexUserInputQuestion(BaseModel):
    model_config = _STRICT_ALIASED

    id: str
    header: str
    question: str
    options: list[CodexUserInputOption] | None = None
    is_other: bool = Field(default=False, alias="isOther")
    is_secret: bool = Field(default=False, alias="isSecret")


class CodexUserInputParams(CodexServerRequest):
    """Typed params for item/tool/requestUserInput."""

    model_config = _STRICT_ALIASED

    interaction_kind: ClassVar[InteractionKind] = InteractionKind.STRUCTURED_QUESTION

    thread_id: str = Field(alias="threadId")
    turn_id: str = Field(alias="turnId")
    item_id: str = Field(alias="itemId")
    questions: list[CodexUserInputQuestion]
    auto_resolution_ms: int | None = Field(default=None, alias="autoResolutionMs")

    def canonical_questions(self) -> tuple[CanonicalQuestion, ...]:
        return canonical_questions(
            [item.model_dump(by_alias=True, exclude_none=True) for item in self.questions]
        )

    def to_request(self) -> StructuredQuestionPayload:
        return StructuredQuestionPayload(questions=self.canonical_questions())

    def to_native_result(self, answer: InteractionAnswer) -> dict[str, Any]:
        values = canonical_answer_values(answer, self.canonical_questions())
        return {
            "answers": {
                question_id: {"answers": selected} for question_id, selected in values.items()
            }
        }


CodexServerRequestParams = CodexApprovalParams | CodexUserInputParams

_COMMAND_APPROVAL_METHOD = "item/commandExecution/requestApproval"
_FILE_APPROVAL_METHOD = "item/fileChange/requestApproval"
_USER_INPUT_METHOD = "item/tool/requestUserInput"
_MCP_ELICITATION_METHOD = "mcpServer/elicitation/request"


def parse_codex_approval_params(method: str, params: dict[str, Any] | None) -> CodexApprovalParams:
    raw = params or {}
    if method == _COMMAND_APPROVAL_METHOD:
        return CodexCommandApprovalParams.model_validate(raw)
    if method == _FILE_APPROVAL_METHOD:
        return CodexFileApprovalParams.model_validate(raw)
    if method == _MCP_ELICITATION_METHOD:
        return CodexMcpToolApprovalParams.model_validate(raw)
    raise ValueError(f"unsupported codex approval method: {method!r}")


def parse_codex_server_request_params(
    method: str,
    params: dict[str, Any] | None,
) -> CodexServerRequestParams:
    if method == _USER_INPUT_METHOD:
        return CodexUserInputParams.model_validate(params or {})
    return parse_codex_approval_params(method, params)


CodexNotification = (
    CodexAgentMessageDelta
    | CodexReasoningDelta
    | CodexTurnStarted
    | CodexError
    | CodexTurnCompleted
    | CodexTokenUsageUpdated
    | CodexItemStarted
    | CodexItemCompleted
)


def parse_codex_notification(raw: dict[str, Any]) -> CodexNotification:
    method = raw.get("method")
    if method == "agentMessageDelta":
        return CodexAgentMessageDelta.model_validate(raw)
    if method == "reasoningDelta":
        return CodexReasoningDelta.model_validate(raw)
    if method == "turnStarted":
        return CodexTurnStarted.model_validate(raw)
    if method == "error":
        return CodexError.model_validate(raw)
    if method == "turnCompleted":
        return CodexTurnCompleted.model_validate(raw)
    if method == "tokenUsageUpdated":
        return CodexTokenUsageUpdated.model_validate(raw)
    if method == "itemStarted":
        return CodexItemStarted.model_validate(raw)
    if method == "itemCompleted":
        return CodexItemCompleted.model_validate(raw)
    raise ValueError(f"unsupported codex notification method: {method!r}")

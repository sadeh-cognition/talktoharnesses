"""Strict adapter-owned Codex notification and approval schemas."""

from __future__ import annotations

import shlex
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator

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


class CodexTurnCompleted(BaseModel):
    model_config = _STRICT

    method: Literal["turnCompleted"] = "turnCompleted"
    thread_id: str
    turn_id: str
    status: str
    final_response: str | None = None
    error_message: str | None = None
    usage: CodexTurnUsage | None = None


class CodexItemStarted(BaseModel):
    model_config = _STRICT

    method: Literal["itemStarted"] = "itemStarted"
    thread_id: str
    turn_id: str
    item_id: str
    item_type: str
    title: str | None = None


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


class CodexCommandApprovalParams(BaseModel):
    """Typed params for item/commandExecution/requestApproval."""

    model_config = _STRICT_ALIASED

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


class CodexFileChangeEntry(BaseModel):
    model_config = _STRICT_ALIASED

    path: str
    kind: str | None = None


class CodexFileApprovalParams(BaseModel):
    """Typed params for item/fileChange/requestApproval."""

    model_config = _STRICT_ALIASED

    thread_id: str | None = Field(default=None, alias="threadId")
    turn_id: str | None = Field(default=None, alias="turnId")
    item_id: str | None = Field(default=None, alias="itemId")
    files: list[CodexFileChangeEntry] | None = None
    reason: str | None = None


CodexApprovalParams = CodexCommandApprovalParams | CodexFileApprovalParams

_COMMAND_APPROVAL_METHOD = "item/commandExecution/requestApproval"
_FILE_APPROVAL_METHOD = "item/fileChange/requestApproval"


def parse_codex_approval_params(method: str, params: dict[str, Any] | None) -> CodexApprovalParams:
    raw = params or {}
    if method == _COMMAND_APPROVAL_METHOD:
        return CodexCommandApprovalParams.model_validate(raw)
    if method == _FILE_APPROVAL_METHOD:
        return CodexFileApprovalParams.model_validate(raw)
    raise ValueError(f"unsupported codex approval method: {method!r}")


CodexNotification = (
    CodexAgentMessageDelta
    | CodexReasoningDelta
    | CodexTurnStarted
    | CodexTurnCompleted
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
    if method == "turnCompleted":
        return CodexTurnCompleted.model_validate(raw)
    if method == "itemStarted":
        return CodexItemStarted.model_validate(raw)
    if method == "itemCompleted":
        return CodexItemCompleted.model_validate(raw)
    raise ValueError(f"unsupported codex notification method: {method!r}")

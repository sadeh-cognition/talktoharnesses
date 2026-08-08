"""Strict adapter-owned Codex notification and approval schemas."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

_STRICT = ConfigDict(extra="forbid", frozen=True)


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

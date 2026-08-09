"""Strict adapter-owned Claude Agent SDK message schemas."""

from __future__ import annotations

from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field

_STRICT = ConfigDict(extra="forbid", frozen=True)


class ClaudeTextBlock(BaseModel):
    model_config = _STRICT

    type: Literal["text"] = "text"
    text: str


class ClaudeThinkingBlock(BaseModel):
    model_config = _STRICT

    type: Literal["thinking"] = "thinking"
    thinking: str


class ClaudeToolUseBlock(BaseModel):
    model_config = _STRICT

    type: Literal["tool_use"] = "tool_use"
    id: str
    name: str
    input: dict[str, Any] = Field(default_factory=dict)


class ClaudeToolResultBlock(BaseModel):
    model_config = _STRICT

    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: str | None = None
    is_error: bool | None = None


ClaudeContentBlock = (
    ClaudeTextBlock | ClaudeThinkingBlock | ClaudeToolUseBlock | ClaudeToolResultBlock
)


class ClaudeSystemMessage(BaseModel):
    model_config = _STRICT

    type: Literal["system"] = "system"
    subtype: str
    data: dict[str, Any] = Field(default_factory=dict)
    session_id: str | None = None


class ClaudeAssistantMessage(BaseModel):
    model_config = _STRICT

    type: Literal["assistant"] = "assistant"
    content: list[ClaudeContentBlock]
    model: str = ""
    parent_tool_use_id: str | None = None
    session_id: str | None = None
    message_id: str | None = None


class ClaudeUserMessage(BaseModel):
    model_config = _STRICT

    type: Literal["user"] = "user"
    content: list[ClaudeContentBlock] | str = ""
    session_id: str | None = None


class ClaudeResultMessage(BaseModel):
    model_config = _STRICT

    type: Literal["result"] = "result"
    subtype: str
    session_id: str
    is_error: bool = False
    duration_ms: int = 0
    duration_api_ms: int = 0
    num_turns: int = 0
    stop_reason: str | None = None
    total_cost_usd: float | None = None
    usage: dict[str, Any] | None = None
    result: str | None = None
    errors: list[str] | None = None


ClaudeMessage = (
    ClaudeSystemMessage | ClaudeAssistantMessage | ClaudeUserMessage | ClaudeResultMessage
)


def parse_claude_message(raw: dict[str, Any]) -> ClaudeMessage:
    msg_type = raw.get("type")
    if msg_type == "system":
        return ClaudeSystemMessage.model_validate(raw)
    if msg_type == "assistant":
        return ClaudeAssistantMessage.model_validate(_normalize_assistant(raw))
    if msg_type == "user":
        return ClaudeUserMessage.model_validate(_normalize_assistant(raw))
    if msg_type == "result":
        return ClaudeResultMessage.model_validate(raw)
    raise ValueError(f"unsupported claude message type: {msg_type!r}")


def _normalize_assistant(raw: dict[str, Any]) -> dict[str, Any]:
    content = raw.get("content")
    if not isinstance(content, list):
        return raw
    normalized: list[dict[str, Any]] = []
    for item_obj in cast(list[object], content):
        if not isinstance(item_obj, dict):
            continue
        item = cast(dict[str, Any], item_obj)
        block_type = item.get("type")
        if block_type == "text":
            normalized.append({"type": "text", "text": str(item.get("text", ""))})
        elif block_type == "thinking":
            normalized.append({"type": "thinking", "thinking": str(item.get("thinking", ""))})
        elif block_type == "tool_use":
            inp = item.get("input")
            normalized.append(
                {
                    "type": "tool_use",
                    "id": str(item.get("id", "")),
                    "name": str(item.get("name", "")),
                    "input": inp if isinstance(inp, dict) else {},
                }
            )
        elif block_type == "tool_result":
            content_val = item.get("content")
            is_error = item.get("is_error")
            normalized.append(
                {
                    "type": "tool_result",
                    "tool_use_id": str(item.get("tool_use_id", "")),
                    "content": content_val if isinstance(content_val, str) else None,
                    "is_error": is_error if isinstance(is_error, bool) else None,
                }
            )
        else:
            raise ValueError(f"unsupported content block type: {block_type!r}")
    out = dict(raw)
    out["content"] = normalized
    return out

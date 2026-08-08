"""Claude Agent SDK messages → canonical HarnessEvent normalization."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID, uuid4, uuid5

from talktoharnesses.domain.enums import (
    ApprovalDecision,
    ErrorCode,
    InteractionKind,
    ToolOutcome,
)
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.domain.events import (
    AssistantMessageCompletedPayload,
    AssistantMessageDeltaPayload,
    AssistantMessageStartedPayload,
    HarnessEvent,
    InteractionRequestedPayload,
    ReasoningCompletedPayload,
    ReasoningDeltaPayload,
    ReasoningStartedPayload,
    ToolCompletedPayload,
    ToolRequestedPayload,
    ToolStartedPayload,
    TurnCompletedPayload,
    TurnFailedPayload,
    UsageUpdatedPayload,
)
from talktoharnesses.domain.models import ApprovalRequestPayload, CanonicalToolResult
from talktoharnesses.providers.claude.schemas import (
    ClaudeAssistantMessage,
    ClaudeMessage,
    ClaudeResultMessage,
    ClaudeSystemMessage,
    ClaudeTextBlock,
    ClaudeThinkingBlock,
    ClaudeToolResultBlock,
    ClaudeToolUseBlock,
    parse_claude_message,
)

_NS = UUID("c9e5a1b3-4d6f-508b-ac2e-3f4a5b6c7d8e")


def _stable_uuid(native_key: str) -> UUID:
    return uuid5(_NS, native_key)


class ClaudeNormalizer:
    def __init__(self) -> None:
        self._native_session_id: str | None = None
        self._active_turn_id: UUID | None = None
        self._resync_mode = False
        self._message_id: UUID | None = None
        self._message_text = ""
        self._message_seq = 0
        self._has_assistant_message = False
        self._reasoning_id: UUID | None = None
        self._reasoning_text = ""
        self._tools: dict[str, UUID] = {}
        self._tool_names: dict[str, str] = {}
        self._seen_native_ids: set[str] = set()
        self._seen_offsets: set[str] = set()
        self._redaction_patterns: tuple[str, ...] = ()

    def set_redaction_patterns(self, patterns: Sequence[str]) -> None:
        self._redaction_patterns = tuple(sorted((p for p in patterns if p), key=len, reverse=True))

    def set_session(self, native_session_id: str, *, resync: bool = False) -> None:
        self._native_session_id = native_session_id
        self._resync_mode = resync

    def begin_turn(self, turn_id: UUID) -> None:
        self._active_turn_id = turn_id
        self._message_id = None
        self._message_text = ""
        self._message_seq = 0
        self._has_assistant_message = False
        self._reasoning_id = None
        self._reasoning_text = ""

    def import_seen(
        self,
        native_ids: frozenset[str],
        stream_offsets: frozenset[str],
    ) -> None:
        self._seen_native_ids.update(native_ids)
        self._seen_offsets.update(stream_offsets)

    def export_seen(self) -> tuple[frozenset[str], frozenset[str]]:
        return frozenset(self._seen_native_ids), frozenset(self._seen_offsets)

    def on_message(self, raw: dict[str, Any] | ClaudeMessage) -> list[HarnessEvent]:
        msg = raw if not isinstance(raw, dict) else parse_claude_message(raw)
        if isinstance(msg, ClaudeSystemMessage):
            return []
        if isinstance(msg, ClaudeAssistantMessage):
            return self._assistant(msg)
        if isinstance(msg, ClaudeResultMessage):
            return self._result(msg)
        return []

    def on_permission_request(
        self,
        *,
        tool_name: str,
        tool_input: dict[str, Any],
        interaction_id: UUID,
        tool_use_id: str | None = None,
    ) -> list[HarnessEvent]:
        if self._active_turn_id is None:
            raise DomainError(ErrorCode.INVALID_STATE, "permission without active turn")
        del tool_input, tool_use_id
        return [
            InteractionRequestedPayload(
                turn_id=self._active_turn_id,
                interaction_id=interaction_id,
                kind=InteractionKind.APPROVAL,
                request=ApprovalRequestPayload(
                    tool_name=tool_name,
                    summary=f"Claude tool permission: {tool_name}",
                    available_decisions=(
                        ApprovalDecision.ALLOW_ONCE,
                        ApprovalDecision.ALLOW_SESSION,
                        ApprovalDecision.DENY,
                        ApprovalDecision.CANCEL,
                    ),
                ),
            )
        ]

    def fail_active_turn(self, *, error_code: str, message: str) -> list[HarnessEvent]:
        """Close open streams and fail the active turn."""
        if self._active_turn_id is None:
            return []
        events = self._close_open_streams()
        events.append(
            TurnFailedPayload(
                turn_id=self._active_turn_id,
                error_code=error_code,
                message=message,
            )
        )
        self._active_turn_id = None
        return events

    def _assistant(self, msg: ClaudeAssistantMessage) -> list[HarnessEvent]:
        if self._active_turn_id is None or self._resync_mode:
            return []
        events: list[HarnessEvent] = []
        for block in msg.content:
            if isinstance(block, ClaudeTextBlock):
                events.extend(self._text(block.text, msg.message_id))
            elif isinstance(block, ClaudeThinkingBlock):
                events.extend(self._thinking(block.thinking))
            elif isinstance(block, ClaudeToolUseBlock):
                events.extend(self._tool_use(block))
            else:
                events.extend(self._tool_result(block))
        return events

    def _text(self, text: str, message_id: str | None) -> list[HarnessEvent]:
        assert self._active_turn_id is not None
        events: list[HarnessEvent] = []
        key = message_id or "assistant"
        if self._message_id is None:
            self._message_id = _stable_uuid(f"msg:{key}")
            self._has_assistant_message = True
            events.append(
                AssistantMessageStartedPayload(
                    turn_id=self._active_turn_id,
                    message_id=self._message_id,
                )
            )
        redacted = self._redact(text)
        if redacted:
            self._message_seq += 1
            self._message_text += redacted
            events.append(
                AssistantMessageDeltaPayload(
                    turn_id=self._active_turn_id,
                    message_id=self._message_id,
                    sequence=self._message_seq,
                    text=redacted,
                )
            )
        return events

    def _thinking(self, text: str) -> list[HarnessEvent]:
        assert self._active_turn_id is not None
        events: list[HarnessEvent] = []
        if self._reasoning_id is None:
            self._reasoning_id = uuid4()
            events.append(
                ReasoningStartedPayload(
                    turn_id=self._active_turn_id,
                    reasoning_id=self._reasoning_id,
                )
            )
        redacted = self._redact(text)
        assert self._reasoning_id is not None
        if redacted:
            self._reasoning_text += redacted
            events.append(
                ReasoningDeltaPayload(
                    turn_id=self._active_turn_id,
                    reasoning_id=self._reasoning_id,
                    text=redacted,
                )
            )
        return events

    def _tool_use(self, block: ClaudeToolUseBlock) -> list[HarnessEvent]:
        assert self._active_turn_id is not None
        tool_id = _stable_uuid(f"tool:{block.id}")
        self._tools[block.id] = tool_id
        self._tool_names[block.id] = block.name
        return [
            ToolRequestedPayload(
                turn_id=self._active_turn_id,
                tool_id=tool_id,
                tool_name=block.name,
                arguments=dict(block.input),
            ),
            ToolStartedPayload(
                turn_id=self._active_turn_id,
                tool_id=tool_id,
                tool_name=block.name,
            ),
        ]

    def _tool_result(self, block: ClaudeToolResultBlock) -> list[HarnessEvent]:
        assert self._active_turn_id is not None
        tool_id = self._tools.get(block.tool_use_id)
        if tool_id is None:
            return []
        name = self._tool_names.get(block.tool_use_id, "tool")
        outcome = ToolOutcome.FAILURE if block.is_error else ToolOutcome.SUCCESS
        output_tail = CanonicalToolResult(
            turn_id=self._active_turn_id,
            tool_name=name,
            outcome=outcome,
            output_tail=self._redact(block.content or ""),
        ).output_tail
        return [
            ToolCompletedPayload(
                turn_id=self._active_turn_id,
                tool_id=tool_id,
                tool_name=name,
                outcome=outcome,
                output_tail=output_tail,
            )
        ]

    def _result(self, msg: ClaudeResultMessage) -> list[HarnessEvent]:
        if self._active_turn_id is None:
            raise DomainError(ErrorCode.INVALID_STATE, "result without active turn")
        if self._native_session_id and msg.session_id != self._native_session_id:
            raise DomainError(
                ErrorCode.PROTOCOL_ERROR,
                "claude result session_id mismatch",
                details={"expected": self._native_session_id, "got": msg.session_id},
            )
        events = self._close_open_streams()
        if msg.usage:
            events.append(
                UsageUpdatedPayload(
                    turn_id=self._active_turn_id,
                    input_tokens=_as_int(msg.usage.get("input_tokens")),
                    output_tokens=_as_int(msg.usage.get("output_tokens")),
                    total_tokens=_as_int(msg.usage.get("total_tokens")),
                )
            )
        if msg.is_error:
            err = "; ".join(msg.errors or []) or "claude turn failed"
            events.append(
                TurnFailedPayload(
                    turn_id=self._active_turn_id,
                    error_code="provider_error",
                    message=err,
                )
            )
        else:
            events.append(
                TurnCompletedPayload(
                    turn_id=self._active_turn_id,
                    terminal_reason=msg.stop_reason or msg.subtype,
                    has_assistant_message=self._has_assistant_message or bool(msg.result),
                )
            )
        self._active_turn_id = None
        return events

    def _close_open_streams(self) -> list[HarnessEvent]:
        assert self._active_turn_id is not None
        events: list[HarnessEvent] = []
        if self._reasoning_id is not None:
            events.append(
                ReasoningCompletedPayload(
                    turn_id=self._active_turn_id,
                    reasoning_id=self._reasoning_id,
                    text=self._reasoning_text,
                )
            )
            self._reasoning_id = None
            self._reasoning_text = ""
        if self._message_id is not None:
            events.append(
                AssistantMessageCompletedPayload(
                    turn_id=self._active_turn_id,
                    message_id=self._message_id,
                    text=self._message_text,
                )
            )
            self._message_id = None
            self._message_text = ""
        return events

    def _redact(self, text: str) -> str:
        out = text
        for pattern in self._redaction_patterns:
            if pattern:
                out = out.replace(pattern, "***")
        return out


def _as_int(value: object) -> int | None:
    if isinstance(value, int):
        return value
    return None

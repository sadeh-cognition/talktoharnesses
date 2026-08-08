"""Codex native notifications → canonical HarnessEvent normalization."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID, uuid5

from talktoharnesses.domain.enums import (
    ErrorCode,
    ToolOutcome,
)
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.domain.events import (
    AssistantMessageCompletedPayload,
    AssistantMessageDeltaPayload,
    AssistantMessageStartedPayload,
    HarnessEvent,
    ReasoningCompletedPayload,
    ReasoningDeltaPayload,
    ReasoningStartedPayload,
    ToolCompletedPayload,
    ToolRequestedPayload,
    ToolStartedPayload,
    TurnCompletedPayload,
    TurnFailedPayload,
    TurnInterruptedPayload,
    UsageUpdatedPayload,
)
from talktoharnesses.providers.codex.schemas import (
    CodexAgentMessageDelta,
    CodexItemCompleted,
    CodexItemStarted,
    CodexNotification,
    CodexReasoningDelta,
    CodexTurnCompleted,
    parse_codex_notification,
)

_NS = UUID("b8d4f0a2-3c5e-4f7a-9b1d-2e3f4a5b6c7d")


def _stable_uuid(native_key: str) -> UUID:
    return uuid5(_NS, native_key)


class CodexNormalizer:
    """One normalizer instance per Codex adapter/runtime."""

    def __init__(self) -> None:
        self._native_session_id: str | None = None
        self._active_turn_id: UUID | None = None
        self._resync_mode = False
        self._message_id: UUID | None = None
        self._message_text = ""
        self._message_seq = 0
        self._reasoning_id: UUID | None = None
        self._reasoning_text = ""
        self._tools: dict[str, UUID] = {}
        self._tool_names: dict[str, str] = {}
        self._seen_native_ids: set[str] = set()
        self._seen_offsets: set[str] = set()
        self._redaction_patterns: tuple[str, ...] = ()
        self._has_assistant_message = False

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
        self._reasoning_id = None
        self._reasoning_text = ""
        self._has_assistant_message = False

    def import_seen(
        self,
        native_ids: frozenset[str],
        stream_offsets: frozenset[str],
    ) -> None:
        self._seen_native_ids.update(native_ids)
        self._seen_offsets.update(stream_offsets)

    def export_seen(self) -> tuple[frozenset[str], frozenset[str]]:
        return frozenset(self._seen_native_ids), frozenset(self._seen_offsets)

    def on_notification(self, raw: dict[str, Any] | CodexNotification) -> list[HarnessEvent]:
        note = raw if not isinstance(raw, dict) else parse_codex_notification(raw)
        if self._native_session_id is None:
            raise DomainError(ErrorCode.INVALID_STATE, "codex normalizer has no session")
        thread_id = getattr(note, "thread_id", None)
        if isinstance(thread_id, str) and thread_id != self._native_session_id:
            raise DomainError(
                ErrorCode.PROTOCOL_ERROR,
                "codex notification thread_id mismatch",
                details={"expected": self._native_session_id, "got": thread_id},
            )
        if isinstance(note, CodexAgentMessageDelta):
            return self._message_delta(note)
        if isinstance(note, CodexReasoningDelta):
            return self._reasoning_delta(note)
        if isinstance(note, CodexItemStarted):
            return self._item_started(note)
        if isinstance(note, CodexItemCompleted):
            return self._item_completed(note)
        if isinstance(note, CodexTurnCompleted):
            return self._turn_completed(note)
        return []

    def fail_active_turn(self, *, error_code: str, message: str) -> list[HarnessEvent]:
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

    def _message_delta(self, note: CodexAgentMessageDelta) -> list[HarnessEvent]:
        if self._active_turn_id is None or self._resync_mode:
            return []
        message_key = f"msg:{note.item_id}"
        sequence = self._message_seq + 1
        offset_key = f"{message_key}:{sequence}"
        if offset_key in self._seen_offsets:
            self._message_seq = sequence
            return []
        events: list[HarnessEvent] = []
        if self._message_id is None:
            self._message_id = _stable_uuid(message_key)
            self._has_assistant_message = True
            events.append(
                AssistantMessageStartedPayload(
                    turn_id=self._active_turn_id,
                    message_id=self._message_id,
                )
            )
        text = self._redact(note.delta)
        if text:
            self._message_seq = sequence
            self._message_text += text
            events.append(
                AssistantMessageDeltaPayload(
                    turn_id=self._active_turn_id,
                    message_id=self._message_id,
                    sequence=self._message_seq,
                    text=text,
                )
            )
        self._seen_offsets.add(offset_key)
        return events

    def _reasoning_delta(self, note: CodexReasoningDelta) -> list[HarnessEvent]:
        if self._active_turn_id is None or self._resync_mode:
            return []
        events: list[HarnessEvent] = []
        if self._reasoning_id is None:
            self._reasoning_id = _stable_uuid(f"reason:{note.item_id}")
            events.append(
                ReasoningStartedPayload(
                    turn_id=self._active_turn_id,
                    reasoning_id=self._reasoning_id,
                )
            )
        text = self._redact(note.delta)
        assert self._reasoning_id is not None
        if text:
            self._reasoning_text += text
            events.append(
                ReasoningDeltaPayload(
                    turn_id=self._active_turn_id,
                    reasoning_id=self._reasoning_id,
                    text=text,
                )
            )
        return events

    def _item_started(self, note: CodexItemStarted) -> list[HarnessEvent]:
        if self._active_turn_id is None or self._resync_mode:
            return []
        if note.item_type not in {"command", "tool", "file"}:
            return []
        tool_id = _stable_uuid(f"tool:{note.item_id}")
        self._tools[note.item_id] = tool_id
        name = note.title or note.item_type
        self._tool_names[note.item_id] = name
        return [
            ToolRequestedPayload(
                turn_id=self._active_turn_id,
                tool_id=tool_id,
                tool_name=name,
            ),
            ToolStartedPayload(
                turn_id=self._active_turn_id,
                tool_id=tool_id,
                tool_name=name,
            ),
        ]

    def _item_completed(self, note: CodexItemCompleted) -> list[HarnessEvent]:
        if self._active_turn_id is None or self._resync_mode:
            return []
        tool_id = self._tools.get(note.item_id)
        if tool_id is None:
            return []
        name = self._tool_names.get(note.item_id, note.item_type)
        outcome = ToolOutcome.SUCCESS
        if note.status and note.status.lower() in {"failed", "error"}:
            outcome = ToolOutcome.FAILURE
        return [
            ToolCompletedPayload(
                turn_id=self._active_turn_id,
                tool_id=tool_id,
                tool_name=name,
                outcome=outcome,
            )
        ]

    def _turn_completed(self, note: CodexTurnCompleted) -> list[HarnessEvent]:
        if self._active_turn_id is None:
            return []
        events: list[HarnessEvent] = []
        events.extend(self._close_open_streams())
        if note.usage is not None:
            events.append(
                UsageUpdatedPayload(
                    turn_id=self._active_turn_id,
                    input_tokens=note.usage.input_tokens,
                    output_tokens=note.usage.output_tokens,
                    total_tokens=note.usage.total_tokens,
                    cached_input_tokens=note.usage.cached_input_tokens,
                )
            )
        status = note.status.lower()
        if status in {"interrupted", "cancelled"}:
            events.append(
                TurnInterruptedPayload(
                    turn_id=self._active_turn_id,
                    reason=status,
                )
            )
        elif status in {"failed", "error"}:
            events.append(
                TurnFailedPayload(
                    turn_id=self._active_turn_id,
                    error_code="provider_error",
                    message=note.error_message or "codex turn failed",
                )
            )
        else:
            events.append(
                TurnCompletedPayload(
                    turn_id=self._active_turn_id,
                    terminal_reason=status,
                    has_assistant_message=self._has_assistant_message or bool(note.final_response),
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

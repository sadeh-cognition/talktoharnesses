"""Prime Agent RPC events to canonical harness events."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast
from uuid import UUID, uuid5

from tth_types.enums import ErrorCode, InteractionKind, ToolOutcome
from tth_types.errors import DomainError
from tth_types.events import (
    AssistantMessageCompletedPayload,
    AssistantMessageDeltaPayload,
    AssistantMessageStartedPayload,
    HarnessEvent,
    InteractionRequestedPayload,
    ProviderWarningPayload,
    ReasoningCompletedPayload,
    ReasoningDeltaPayload,
    ReasoningStartedPayload,
    ToolCompletedPayload,
    ToolOutputDeltaPayload,
    ToolRequestedPayload,
    ToolStartedPayload,
    TurnCompletedPayload,
    TurnFailedPayload,
    TurnInterruptedPayload,
    TurnOutcomeUnknownPayload,
    UsageUpdatedPayload,
)
from tth_types.harness import CanonicalQuestion, StructuredQuestionPayload

_NS = UUID("86eb9dd2-4c63-5c51-8ea4-e02f20cc742c")
_IGNORED_EVENTS = {
    "agent_start",
    "turn_start",
    "turn_end",
    "session_action_update",
    "compaction_start",
    "compaction_end",
    "auto_retry_end",
}
_USAGE_KEYS = {
    "input": "input_tokens",
    "output": "output_tokens",
    "totalTokens": "total_tokens",
    "cacheRead": "cached_input_tokens",
}


def _stable_uuid(value: str) -> UUID:
    return uuid5(_NS, value)


def _mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in cast(dict[object, object], value).items()}


def _result_text(value: object) -> str:
    result = _mapping(value)
    content = result.get("content")
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in cast(list[object], content):
        mapped = _mapping(item)
        if mapped.get("type") == "text" and isinstance(mapped.get("text"), str):
            parts.append(cast(str, mapped["text"]))
    return "".join(parts)


def _optional_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


class PrimeAgentNormalizer:
    def __init__(self) -> None:
        self._active_turn_id: UUID | None = None
        self._message_id: UUID | None = None
        self._message_index = 0
        self._message_text = ""
        self._message_sequence = 0
        self._reasoning_id: UUID | None = None
        self._reasoning_index = 0
        self._reasoning_text = ""
        self._tools: dict[str, tuple[UUID, str, str, int]] = {}
        self._has_assistant_message = False
        self._pending_terminal: TurnFailedPayload | TurnInterruptedPayload | None = None
        self._interrupt_requested = False
        self._awaiting_prompt_ack = False
        self._usage: dict[str, int] = {}
        self._usage_message_ids: set[str] = set()
        self._redaction_patterns: tuple[str, ...] = ()

    @property
    def turn_active(self) -> bool:
        return self._active_turn_id is not None

    def prompt_accepted(self) -> None:
        """The CLI acknowledged this turn's prompt; terminals now belong to it.

        A busy CLI can still be finishing the previous turn's residual
        processing when the next turn begins (`prompt` is retried while the
        agent reports "already processing"); the residual ``agent_end`` must
        not terminate the new turn.
        """
        self._awaiting_prompt_ack = False

    def note_interrupt_requested(self) -> None:
        """Record that the adapter sent ``abort`` for the active turn.

        Prime Agent ends an aborted turn with plain ``turn_end``/``agent_end``
        frames and no aborted marker, so the terminal classification has to
        come from the adapter's own knowledge of the abort request.
        """
        if self._active_turn_id is not None:
            self._interrupt_requested = True

    def set_redaction_patterns(self, patterns: Sequence[str]) -> None:
        self._redaction_patterns = tuple(sorted((p for p in patterns if p), key=len, reverse=True))

    def begin_turn(self, turn_id: UUID) -> None:
        if self._active_turn_id is not None:
            raise DomainError(ErrorCode.INVALID_STATE, "prime agent turn already active")
        self._active_turn_id = turn_id
        self._message_id = None
        self._message_index = 0
        self._message_text = ""
        self._message_sequence = 0
        self._reasoning_id = None
        self._reasoning_index = 0
        self._reasoning_text = ""
        self._tools.clear()
        self._has_assistant_message = False
        self._pending_terminal = None
        self._interrupt_requested = False
        self._awaiting_prompt_ack = True
        self._usage.clear()
        self._usage_message_ids.clear()

    def on_event(self, raw: dict[str, Any]) -> list[HarnessEvent]:
        event_type = raw.get("type")
        if not isinstance(event_type, str):
            raise DomainError(ErrorCode.PROTOCOL_ERROR, "prime agent event missing type")
        if event_type in _IGNORED_EVENTS:
            return []
        if event_type == "message_update":
            return self._message_update(_mapping(raw.get("assistantMessageEvent")))
        if event_type == "message_start":
            return []
        if event_type == "message_end":
            self._record_usage(_mapping(raw.get("message")))
            return self._close_streams()
        if event_type == "tool_execution_start":
            return self._tool_start(raw)
        if event_type == "tool_execution_update":
            return self._tool_update(raw)
        if event_type == "tool_execution_end":
            return self._tool_end(raw)
        if event_type == "agent_end":
            return self._agent_end()
        if event_type == "auto_retry_start":
            self._pending_terminal = None
            return []
        if event_type == "extension_error":
            message = self._redact(str(raw.get("error") or "Prime Agent extension error"))
            return [ProviderWarningPayload(message=message)]
        raise DomainError(
            ErrorCode.UNSUPPORTED_NATIVE_EVENT,
            f"unsupported prime agent event type: {event_type}",
        )

    def on_outcome_unknown(self, message: str) -> list[HarnessEvent]:
        if self._active_turn_id is None:
            return []
        turn_id = self._active_turn_id
        events = self._close_streams()
        events.append(TurnOutcomeUnknownPayload(turn_id=turn_id, message=message))
        self._active_turn_id = None
        self._pending_terminal = None
        return events

    def on_question(
        self,
        questions: tuple[CanonicalQuestion, ...],
        *,
        interaction_id: UUID,
    ) -> list[HarnessEvent]:
        if self._active_turn_id is None:
            raise DomainError(ErrorCode.INVALID_STATE, "question without active turn")
        return [
            InteractionRequestedPayload(
                turn_id=self._active_turn_id,
                interaction_id=interaction_id,
                kind=InteractionKind.STRUCTURED_QUESTION,
                request=StructuredQuestionPayload(questions=questions),
            )
        ]

    def _message_update(self, event: dict[str, Any]) -> list[HarnessEvent]:
        if self._active_turn_id is None:
            return []
        kind = str(event.get("type") or "")
        if kind in {"start", "text_start", "text_end", "thinking_start", "thinking_end", "done"}:
            return []
        if kind == "text_delta":
            delta = event.get("delta")
            return self._text_delta(delta if isinstance(delta, str) else "")
        if kind == "thinking_delta":
            delta = event.get("delta")
            return self._thinking_delta(delta if isinstance(delta, str) else "")
        if kind in {"toolcall_start", "toolcall_delta", "toolcall_end"}:
            return []
        if kind == "error":
            reason = str(event.get("reason") or "error").lower()
            turn_id = self._active_turn_id
            events = self._close_streams()
            if reason == "aborted":
                self._pending_terminal = TurnInterruptedPayload(turn_id=turn_id, reason=reason)
            else:
                self._pending_terminal = TurnFailedPayload(
                    turn_id=turn_id,
                    error_code="provider_error",
                    message=self._redact(str(event.get("error") or "prime agent turn failed")),
                )
            return events
        raise DomainError(
            ErrorCode.UNSUPPORTED_NATIVE_EVENT,
            f"unsupported prime agent message update: {kind}",
        )

    def _text_delta(self, delta: str) -> list[HarnessEvent]:
        if self._active_turn_id is None or not delta:
            return []
        events: list[HarnessEvent] = []
        if self._message_id is None:
            self._message_index += 1
            self._message_id = _stable_uuid(f"message:{self._active_turn_id}:{self._message_index}")
            self._has_assistant_message = True
            events.append(
                AssistantMessageStartedPayload(
                    turn_id=self._active_turn_id,
                    message_id=self._message_id,
                )
            )
        text = self._redact(delta)
        self._message_sequence += 1
        self._message_text += text
        events.append(
            AssistantMessageDeltaPayload(
                turn_id=self._active_turn_id,
                message_id=self._message_id,
                sequence=self._message_sequence,
                text=text,
            )
        )
        return events

    def _thinking_delta(self, delta: str) -> list[HarnessEvent]:
        if self._active_turn_id is None or not delta:
            return []
        events: list[HarnessEvent] = []
        if self._reasoning_id is None:
            self._reasoning_index += 1
            self._reasoning_id = _stable_uuid(
                f"reasoning:{self._active_turn_id}:{self._reasoning_index}"
            )
            events.append(
                ReasoningStartedPayload(
                    turn_id=self._active_turn_id,
                    reasoning_id=self._reasoning_id,
                )
            )
        text = self._redact(delta)
        self._reasoning_text += text
        events.append(
            ReasoningDeltaPayload(
                turn_id=self._active_turn_id,
                reasoning_id=self._reasoning_id,
                text=text,
            )
        )
        return events

    def _tool_start(self, raw: dict[str, Any]) -> list[HarnessEvent]:
        if self._active_turn_id is None:
            return []
        native_id = str(raw.get("toolCallId") or "")
        name = str(raw.get("toolName") or "tool")
        if not native_id:
            raise DomainError(ErrorCode.PROTOCOL_ERROR, "prime agent tool event missing toolCallId")
        tool_id = _stable_uuid(f"tool:{native_id}")
        self._tools[native_id] = (tool_id, name, "", 0)
        return [
            ToolRequestedPayload(
                turn_id=self._active_turn_id,
                tool_id=tool_id,
                tool_name=name,
                arguments=self._redact_value(_mapping(raw.get("args"))),
            ),
            ToolStartedPayload(
                turn_id=self._active_turn_id,
                tool_id=tool_id,
                tool_name=name,
            ),
        ]

    def _tool_update(self, raw: dict[str, Any]) -> list[HarnessEvent]:
        return self._tool_output(
            str(raw.get("toolCallId") or ""),
            _result_text(raw.get("partialResult")),
        )

    def _tool_output(self, native_id: str, accumulated: str) -> list[HarnessEvent]:
        if self._active_turn_id is None or native_id not in self._tools:
            return []
        tool_id, name, previous, sequence = self._tools[native_id]
        delta = accumulated[len(previous) :] if accumulated.startswith(previous) else accumulated
        if not delta:
            return []
        sequence += 1
        redacted = self._redact(delta)
        self._tools[native_id] = (tool_id, name, accumulated, sequence)
        return [
            ToolOutputDeltaPayload(
                turn_id=self._active_turn_id,
                tool_id=tool_id,
                sequence=sequence,
                text=redacted,
            )
        ]

    def _tool_end(self, raw: dict[str, Any]) -> list[HarnessEvent]:
        if self._active_turn_id is None:
            return []
        native_id = str(raw.get("toolCallId") or "")
        events = self._tool_output(native_id, _result_text(raw.get("result")))
        tool = self._tools.pop(native_id, None)
        if tool is None:
            return events
        tool_id, name, output, _sequence = tool
        outcome = ToolOutcome.FAILURE if raw.get("isError") is True else ToolOutcome.SUCCESS
        events.append(
            ToolCompletedPayload(
                turn_id=self._active_turn_id,
                tool_id=tool_id,
                tool_name=name,
                outcome=outcome,
                output_tail=self._redact(output),
            )
        )
        return events

    def _agent_end(self) -> list[HarnessEvent]:
        if self._active_turn_id is None:
            return []
        if self._awaiting_prompt_ack:
            # Residual processing from the previous turn ended; drop its
            # terminal instead of attributing it to the not-yet-started turn.
            return self._close_streams()
        turn_id = self._active_turn_id
        events = self._close_streams()
        if self._usage:
            events.append(
                UsageUpdatedPayload(
                    turn_id=turn_id,
                    input_tokens=self._usage.get("input_tokens"),
                    output_tokens=self._usage.get("output_tokens"),
                    total_tokens=self._usage.get("total_tokens"),
                    cached_input_tokens=self._usage.get("cached_input_tokens"),
                )
            )
        if self._pending_terminal is not None:
            events.append(self._pending_terminal)
        elif self._interrupt_requested:
            events.append(TurnInterruptedPayload(turn_id=turn_id, reason="aborted"))
        else:
            events.append(
                TurnCompletedPayload(
                    turn_id=turn_id,
                    terminal_reason="end_turn",
                    has_assistant_message=self._has_assistant_message,
                )
            )
        self._active_turn_id = None
        self._pending_terminal = None
        self._interrupt_requested = False
        return events

    def _record_usage(self, message: dict[str, Any]) -> None:
        if message.get("role") != "assistant":
            return
        native_id = message.get("id")
        if isinstance(native_id, str) and native_id:
            if native_id in self._usage_message_ids:
                return
            self._usage_message_ids.add(native_id)
        usage = _mapping(message.get("usage"))
        for native_key, canonical_key in _USAGE_KEYS.items():
            value = _optional_int(usage.get(native_key))
            if value is not None:
                self._usage[canonical_key] = self._usage.get(canonical_key, 0) + value

    def _close_streams(self) -> list[HarnessEvent]:
        if self._active_turn_id is None:
            return []
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
            self._message_sequence = 0
        return events

    def _redact(self, text: str) -> str:
        result = text
        for pattern in self._redaction_patterns:
            result = result.replace(pattern, "***")
        return result

    def _redact_value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self._redact(value)
        if isinstance(value, dict):
            mapping = cast(dict[object, object], value)
            return {
                str(self._redact_value(key)): self._redact_value(item)
                for key, item in mapping.items()
            }
        if isinstance(value, list):
            return [self._redact_value(item) for item in cast(list[object], value)]
        return value

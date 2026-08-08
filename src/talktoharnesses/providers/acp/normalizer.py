"""Stateful ACP native → canonical HarnessEvent normalization."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any, cast
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
    PlanCreatedPayload,
    PlanUpdatedPayload,
    ReasoningCompletedPayload,
    ReasoningDeltaPayload,
    ReasoningStartedPayload,
    ToolCompletedPayload,
    ToolFailedPayload,
    ToolOutputDeltaPayload,
    ToolRequestedPayload,
    ToolStartedPayload,
    TurnCompletedPayload,
    TurnFailedPayload,
    TurnInterruptedPayload,
    TurnOutcomeUnknownPayload,
    UsageUpdatedPayload,
)
from talktoharnesses.domain.models import (
    ApprovalRequestPayload,
    CanonicalToolResult,
    PlanItem,
)

# Namespace for deriving stable UUIDs from native IDs within a session.
_NS = UUID("a7c3e9f1-2b4d-4e6f-8a0c-1d2e3f4a5b6c")


def _stable_uuid(native_key: str) -> UUID:
    return uuid5(_NS, native_key)


def _as_dict(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    raw = cast(dict[object, object], value)
    return {str(k): v for k, v in raw.items()}


def _as_str(value: object, default: str = "") -> str:
    if value is None:
        return default
    return str(value)


def _as_list(value: object) -> list[object]:
    if not isinstance(value, list):
        return []
    return cast(list[object], value)


class AcpSessionNormalizer:
    """One normalizer instance per adapter/runtime."""

    def __init__(self) -> None:
        self._native_session_id: str | None = None
        self._active_turn_id: UUID | None = None
        self._resync_mode = False
        self._stream_offset = 0
        self._message_id: UUID | None = None
        self._message_native_key: str | None = None
        self._message_text = ""
        self._message_seq = 0
        self._reasoning_id: UUID | None = None
        self._reasoning_text = ""
        self._plan_id: UUID | None = None
        self._tools: dict[str, UUID] = {}
        self._tool_names: dict[str, str] = {}
        self._tool_outputs: dict[str, str] = {}
        self._tool_seqs: dict[str, int] = {}
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
        self._resync_mode = False
        self._close_open_streams_state_only()
        self._plan_id = None

    def end_turn_context(self) -> None:
        self._active_turn_id = None
        self._close_open_streams_state_only()

    def _close_open_streams_state_only(self) -> None:
        self._message_id = None
        self._message_native_key = None
        self._message_text = ""
        self._message_seq = 0
        self._reasoning_id = None
        self._reasoning_text = ""

    def note_seen_native(self, native_id: str) -> bool:
        """Return True if this native id was already seen (duplicate)."""
        if native_id in self._seen_native_ids:
            return True
        self._seen_native_ids.add(native_id)
        return False

    def note_seen_offset(self, offset: str) -> bool:
        if offset in self._seen_offsets:
            return True
        self._seen_offsets.add(offset)
        return False

    def export_seen(self) -> tuple[frozenset[str], frozenset[str]]:
        return frozenset(self._seen_native_ids), frozenset(self._seen_offsets)

    def import_seen(
        self,
        native_ids: Iterable[str] = (),
        stream_offsets: Iterable[str] = (),
    ) -> None:
        self._seen_native_ids.update(native_ids)
        self._seen_offsets.update(stream_offsets)

    def on_session_update(
        self,
        params: dict[str, Any],
    ) -> list[HarnessEvent]:
        session_id = params.get("sessionId")
        if self._native_session_id and session_id not in (None, self._native_session_id):
            raise DomainError(
                ErrorCode.PROTOCOL_ERROR,
                "session/update for mismatched session",
                details={"expected": self._native_session_id, "got": session_id},
            )
        update = _as_dict(params.get("update"))
        if update is None:
            raise DomainError(ErrorCode.PROTOCOL_ERROR, "session/update missing update object")
        kind_obj = update.get("sessionUpdate")
        if not isinstance(kind_obj, str):
            raise DomainError(ErrorCode.PROTOCOL_ERROR, "session/update missing sessionUpdate")
        kind = kind_obj

        self._stream_offset += 1
        offset_key = f"{self._native_session_id}:{self._stream_offset}"
        if self.note_seen_offset(offset_key):
            return []

        if self._resync_mode:
            # History replay on load: record ids/offsets only, no canonical events.
            self._record_resync(update)
            return []

        if self._active_turn_id is None:
            raise DomainError(
                ErrorCode.PROTOCOL_ERROR,
                "session/update without an active turn",
                details={"kind": kind},
            )

        if kind == "agent_message_chunk":
            return self._message_chunk(update)
        if kind == "agent_thought_chunk":
            return self._thought_chunk(update)
        if kind == "tool_call":
            return self._tool_call(update)
        if kind == "tool_call_update":
            return self._tool_call_update(update)
        if kind == "plan":
            return self._plan(update)
        if kind == "usage_update":
            return self._usage(update)
        raise DomainError(
            ErrorCode.UNSUPPORTED_NATIVE_EVENT,
            f"unsupported sessionUpdate: {kind}",
            details={"kind": kind},
        )

    def _record_resync(self, update: dict[str, Any]) -> None:
        native = update.get("toolCallId") or update.get("messageId")
        if isinstance(native, str):
            self.note_seen_native(native)

    def _require_turn(self) -> UUID:
        if self._active_turn_id is None:
            raise DomainError(ErrorCode.NO_ACTIVE_TURN, "no active turn")
        return self._active_turn_id

    def _extract_text(self, update: dict[str, Any]) -> str:
        content = update.get("content")
        if isinstance(content, str):
            return content
        content_dict = _as_dict(content)
        if content_dict is not None:
            text_val = content_dict.get("text")
            if isinstance(text_val, str):
                return text_val
            inner = content_dict.get("content")
            if isinstance(inner, list):
                parts: list[str] = []
                for block_obj in _as_list(cast(object, inner)):
                    block = _as_dict(block_obj)
                    if block is not None and block.get("type") == "text":
                        text = block.get("text")
                        if isinstance(text, str):
                            parts.append(text)
                return "".join(parts)
            thought = content_dict.get("thought")
            if isinstance(thought, str):
                return thought
        text = update.get("text")
        if isinstance(text, str):
            return text
        return ""

    def _message_chunk(self, update: dict[str, Any]) -> list[HarnessEvent]:
        turn_id = self._require_turn()
        text = self._extract_text(update)
        native_key = update.get("messageId")
        events: list[HarnessEvent] = []
        if isinstance(native_key, str):
            if self._message_native_key is not None and native_key != self._message_native_key:
                events.extend(self._complete_message())
            if self._message_id is None:
                self._message_id = _stable_uuid(f"msg:{self._native_session_id}:{native_key}")
                self._message_native_key = native_key
                self.note_seen_native(native_key)
                events.append(
                    AssistantMessageStartedPayload(turn_id=turn_id, message_id=self._message_id)
                )
        elif self._message_id is None:
            offset_id = f"msg-offset:{self._native_session_id}:{self._stream_offset}"
            self._message_id = _stable_uuid(offset_id)
            events.append(
                AssistantMessageStartedPayload(turn_id=turn_id, message_id=self._message_id)
            )

        assert self._message_id is not None
        self._message_seq += 1
        self._message_text += text
        if text:
            events.append(
                AssistantMessageDeltaPayload(
                    turn_id=turn_id,
                    message_id=self._message_id,
                    sequence=self._message_seq,
                    text=text,
                )
            )
        return events

    def _complete_message(self) -> list[HarnessEvent]:
        if self._message_id is None or self._active_turn_id is None:
            self._close_open_streams_state_only()
            return []
        event = AssistantMessageCompletedPayload(
            turn_id=self._active_turn_id,
            message_id=self._message_id,
            text=self._message_text,
        )
        self._message_id = None
        self._message_native_key = None
        self._message_text = ""
        self._message_seq = 0
        return [event]

    def _thought_chunk(self, update: dict[str, Any]) -> list[HarnessEvent]:
        turn_id = self._require_turn()
        text = self._extract_text(update)
        events: list[HarnessEvent] = []
        if self._reasoning_id is None:
            self._reasoning_id = uuid4()
            events.append(ReasoningStartedPayload(turn_id=turn_id, reasoning_id=self._reasoning_id))
        self._reasoning_text += text
        if text:
            events.append(
                ReasoningDeltaPayload(
                    turn_id=turn_id,
                    reasoning_id=self._reasoning_id,
                    text=text,
                )
            )
        return events

    def _complete_reasoning(self) -> list[HarnessEvent]:
        if self._reasoning_id is None or self._active_turn_id is None:
            self._reasoning_id = None
            self._reasoning_text = ""
            return []
        event = ReasoningCompletedPayload(
            turn_id=self._active_turn_id,
            reasoning_id=self._reasoning_id,
            text=self._reasoning_text,
        )
        self._reasoning_id = None
        self._reasoning_text = ""
        return [event]

    def _tool_call(self, update: dict[str, Any]) -> list[HarnessEvent]:
        turn_id = self._require_turn()
        tool_call_id = update.get("toolCallId")
        if not isinstance(tool_call_id, str):
            raise DomainError(ErrorCode.PROTOCOL_ERROR, "tool_call missing toolCallId")
        if self.note_seen_native(tool_call_id) and tool_call_id in self._tools:
            return []
        tool_id = _stable_uuid(f"tool:{self._native_session_id}:{tool_call_id}")
        self._tools[tool_call_id] = tool_id
        name = str(update.get("title") or update.get("kind") or "tool")
        self._tool_names[tool_call_id] = name
        raw_input = update.get("rawInput") or update.get("arguments")
        arguments = _as_dict(raw_input) if raw_input is not None else {}
        if arguments is None:
            arguments = {"value": raw_input}
        arguments = self._redact(arguments)
        events: list[HarnessEvent] = [
            ToolRequestedPayload(
                turn_id=turn_id,
                tool_id=tool_id,
                tool_name=name,
                arguments=arguments,
            )
        ]
        status = update.get("status")
        if status in ("in_progress", "running", "pending"):
            events.append(ToolStartedPayload(turn_id=turn_id, tool_id=tool_id, tool_name=name))
        return events

    def _redact(self, value: Any) -> Any:
        if isinstance(value, str):
            for pattern in self._redaction_patterns:
                value = value.replace(pattern, "[REDACTED]")
            return value
        if isinstance(value, dict):
            mapping = cast(dict[object, object], value)
            return {str(self._redact(key)): self._redact(item) for key, item in mapping.items()}
        if isinstance(value, list):
            return [self._redact(item) for item in cast(list[object], value)]
        return value

    def _tool_call_update(self, update: dict[str, Any]) -> list[HarnessEvent]:
        turn_id = self._require_turn()
        tool_call_id = update.get("toolCallId")
        if not isinstance(tool_call_id, str):
            raise DomainError(ErrorCode.PROTOCOL_ERROR, "tool_call_update missing toolCallId")
        tool_id = self._tools.get(tool_call_id)
        if tool_id is None:
            tool_id = _stable_uuid(f"tool:{self._native_session_id}:{tool_call_id}")
            self._tools[tool_call_id] = tool_id
        name = self._tool_names.get(tool_call_id, str(update.get("title") or "tool"))
        events: list[HarnessEvent] = []
        status = update.get("status")
        content = update.get("content")
        if isinstance(content, str) and content:
            prev = self._tool_outputs.get(tool_call_id, "")
            self._tool_outputs[tool_call_id] = prev + content
            seq = self._tool_seqs.get(tool_call_id, 0) + 1
            self._tool_seqs[tool_call_id] = seq
            events.append(
                ToolOutputDeltaPayload(
                    turn_id=turn_id,
                    tool_id=tool_id,
                    sequence=seq,
                    text=content,
                )
            )
        if status in ("completed", "success"):
            full = self._tool_outputs.get(tool_call_id, "")
            tail = CanonicalToolResult(
                turn_id=turn_id,
                tool_name=name,
                full_output=full,
                output_tail=full,
            ).output_tail
            events.append(
                ToolCompletedPayload(
                    turn_id=turn_id,
                    tool_id=tool_id,
                    tool_name=name,
                    outcome=ToolOutcome.SUCCESS,
                    output_tail=tail,
                )
            )
        elif status in ("failed", "error"):
            events.append(
                ToolFailedPayload(
                    turn_id=turn_id,
                    tool_id=tool_id,
                    tool_name=name,
                    message=str(update.get("error") or "tool failed"),
                )
            )
        elif status in ("in_progress", "running"):
            events.append(ToolStartedPayload(turn_id=turn_id, tool_id=tool_id, tool_name=name))
        return events

    def _plan(self, update: dict[str, Any]) -> list[HarnessEvent]:
        turn_id = self._require_turn()
        raw_entries = _as_list(update.get("entries") or update.get("items") or [])
        items: list[PlanItem] = []
        for index, entry_obj in enumerate(raw_entries):
            entry = _as_dict(entry_obj)
            if entry is None:
                continue
            status_val = entry.get("status")
            detail_val = entry.get("detail")
            items.append(
                PlanItem(
                    id=_as_str(entry.get("id"), str(index)),
                    title=_as_str(entry.get("title") or entry.get("content")),
                    status=_as_str(status_val) if status_val is not None else None,
                    detail=_as_str(detail_val) if detail_val is not None else None,
                )
            )
        if self._plan_id is None:
            self._plan_id = uuid4()
            return [
                PlanCreatedPayload(
                    turn_id=turn_id,
                    plan_id=self._plan_id,
                    items=tuple(items),
                )
            ]
        return [
            PlanUpdatedPayload(
                turn_id=turn_id,
                plan_id=self._plan_id,
                items=tuple(items),
            )
        ]

    def _usage(self, update: dict[str, Any]) -> list[HarnessEvent]:
        turn_id = self._require_turn()
        return [
            UsageUpdatedPayload(
                turn_id=turn_id,
                input_tokens=_optional_int(update.get("inputTokens") or update.get("input_tokens")),
                output_tokens=_optional_int(
                    update.get("outputTokens") or update.get("output_tokens")
                ),
                total_tokens=_optional_int(update.get("totalTokens") or update.get("total_tokens")),
                cached_input_tokens=_optional_int(
                    update.get("cachedInputTokens") or update.get("cached_input_tokens")
                ),
            )
        ]

    def on_permission_request(
        self,
        params: dict[str, Any],
        *,
        interaction_id: UUID,
    ) -> list[HarnessEvent]:
        turn_id = self._require_turn()
        tool_name: str | None = None
        tool_call = _as_dict(params.get("toolCall"))
        if tool_call is not None:
            tool_name = _as_str(tool_call.get("title") or tool_call.get("kind"), "tool")
        summary_raw = params.get("description") or params.get("summary") or ""
        options_obj = params.get("options")
        options: list[dict[str, Any]] = []
        if isinstance(options_obj, list):
            for item in cast(list[object], options_obj):
                mapped = _as_dict(item)
                if mapped:
                    options.append(mapped)
        available = self._available_decisions(options)
        action = self._normalize_approval_action(params, tool_call)
        command_args: tuple[str, ...] | None = None
        path: str | None = None
        operation = None
        if action is not None:
            from talktoharnesses.domain.models import (
                CommandApprovalAction,
                FileApprovalAction,
            )

            if isinstance(action, CommandApprovalAction):
                command_args = action.argv
            elif isinstance(action, FileApprovalAction):
                path = action.path
                operation = action.operation
        request = ApprovalRequestPayload(
            tool_name=tool_name,
            command_args=command_args,
            path=path,
            operation=operation,
            summary=_as_str(summary_raw) or None,
            action=action,
            available_decisions=available,
        )
        return [
            InteractionRequestedPayload(
                turn_id=turn_id,
                interaction_id=interaction_id,
                kind=InteractionKind.APPROVAL,
                request=request,
            )
        ]

    def _available_decisions(
        self, options: Sequence[dict[str, Any]]
    ) -> tuple[ApprovalDecision, ...]:
        found: list[ApprovalDecision] = []
        for decision in ApprovalDecision:
            mapped = self.map_approval_decision(decision, options)
            outcome = mapped.get("outcome")
            outcome_map = _as_dict(outcome)
            if outcome_map is not None and outcome_map.get("outcome") == "selected":
                found.append(decision)
        found.append(ApprovalDecision.CANCEL)
        return tuple(found)

    def _normalize_approval_action(
        self,
        params: dict[str, Any],
        tool_call: dict[str, Any] | None,
    ) -> Any:
        """Extract typed action only from structured fields — never parse summaries."""
        from talktoharnesses.domain.enums import FileOperation
        from talktoharnesses.domain.models import (
            CommandApprovalAction,
            FileApprovalAction,
            NetworkApprovalAction,
        )

        if params.get("networkAccess") is True or params.get("network") is True:
            return NetworkApprovalAction()

        if tool_call is not None:
            raw_input = tool_call.get("rawInput")
            input_map = _as_dict(raw_input) if raw_input is not None else None
            if input_map is not None:
                if input_map.get("network") is True:
                    return NetworkApprovalAction()
                cmd = input_map.get("command")
                if isinstance(cmd, list):
                    raw_cmd = cast(list[object], cmd)
                    if raw_cmd and all(isinstance(item, str) for item in raw_cmd):
                        return CommandApprovalAction(argv=tuple(cast(list[str], raw_cmd)))
                path = input_map.get("path")
                op_raw = input_map.get("operation")
                if isinstance(path, str) and isinstance(op_raw, str):
                    try:
                        op = FileOperation(op_raw.lower())
                    except ValueError:
                        op = None
                    if op is not None:
                        return FileApprovalAction(path=path, operation=op)

        return None

    def on_prompt_terminal(
        self,
        stop_reason: str,
        *,
        error_message: str | None = None,
    ) -> list[HarnessEvent]:
        turn_id = self._require_turn()
        events: list[HarnessEvent] = []
        events.extend(self._complete_reasoning())
        events.extend(self._complete_message())
        has_assistant = any(isinstance(e, AssistantMessageCompletedPayload) for e in events)
        if stop_reason in {"end_turn", "max_tokens", "max_turn_requests"}:
            events.append(
                TurnCompletedPayload(
                    turn_id=turn_id,
                    terminal_reason=stop_reason,
                    has_assistant_message=has_assistant or bool(self._message_text),
                )
            )
        elif stop_reason == "cancelled":
            events.append(TurnInterruptedPayload(turn_id=turn_id, reason=stop_reason))
        elif stop_reason == "refusal":
            events.append(
                TurnFailedPayload(
                    turn_id=turn_id,
                    error_code="refusal",
                    message=error_message or "model refused",
                )
            )
        else:
            events.append(
                TurnFailedPayload(
                    turn_id=turn_id,
                    error_code="prompt_error",
                    message=error_message or stop_reason,
                )
            )
        self.end_turn_context()
        return events

    def on_prompt_outcome_unknown(self, message: str) -> list[HarnessEvent]:
        turn_id = self._require_turn()
        events: list[HarnessEvent] = []
        events.extend(self._complete_reasoning())
        events.extend(self._complete_message())
        events.append(
            TurnOutcomeUnknownPayload(
                turn_id=turn_id,
                delivery_phase="delivered",
                message=message,
            )
        )
        self.end_turn_context()
        return events

    def map_approval_decision(
        self,
        decision: ApprovalDecision | None,
        options: Sequence[dict[str, Any]],
    ) -> dict[str, Any]:
        """Map canonical approval decision to ACP permission outcome."""
        by_decision: dict[ApprovalDecision, set[str]] = {
            ApprovalDecision.ALLOW_ONCE: {"allow_once", "allow-once"},
            ApprovalDecision.ALLOW_SESSION: {
                "allow_always",
                "allow-always",
                "allow_session",
                "allow-session",
            },
            ApprovalDecision.DENY: {
                "reject_once",
                "reject-once",
                "deny_once",
                "deny-once",
                "reject_always",
                "reject-always",
            },
        }
        kinds: set[str] = by_decision.get(decision, set()) if decision is not None else set()
        for option in options:
            option_id = option.get("optionId") or option.get("option_id")
            kind = option.get("kind")
            if isinstance(option_id, str) and (
                (isinstance(kind, str) and kind.lower() in kinds) or option_id.lower() in kinds
            ):
                return {"outcome": {"outcome": "selected", "optionId": option_id}}
        return {"outcome": {"outcome": "cancelled"}}


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

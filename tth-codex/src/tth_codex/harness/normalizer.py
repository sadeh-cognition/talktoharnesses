"""Codex native notifications → canonical HarnessEvent normalization."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID, uuid5

from tth_types.enums import (
    ApprovalDecision,
    ErrorCode,
    FileOperation,
    InteractionKind,
    ToolOutcome,
)
from tth_types.errors import DomainError
from tth_types.events import (
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
    TurnInterruptedPayload,
)
from tth_types.harness import (
    ApprovalRequestPayload,
    CanonicalQuestion,
    CommandApprovalAction,
    FileApprovalAction,
    StructuredQuestionPayload,
)
from tth_types.usage import TurnUsage

from tth_codex.harness.schemas import (
    CodexAgentMessageDelta,
    CodexApprovalParams,
    CodexCommandApprovalParams,
    CodexItemCompleted,
    CodexItemStarted,
    CodexNotification,
    CodexReasoningDelta,
    CodexTokenUsageUpdated,
    CodexTurnCompleted,
    parse_codex_notification,
)

_NS = UUID("b8d4f0a2-3c5e-4f7a-9b1d-2e3f4a5b6c7d")

_ITEM_TOOL_NAMES = {"command": "commandExecution", "file": "fileChange"}


def _difference(totals: dict[str, Any], baseline: dict[str, Any]) -> dict[str, int | None]:
    """``totals`` less ``baseline``, dropping any category either one omits."""
    difference: dict[str, int | None] = {}
    for name, total in totals.items():
        start = baseline.get(name)
        if type(total) is int and type(start) is int and total >= start:
            difference[name] = total - start
        else:
            difference[name] = None
    return difference


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
        self._usage = TurnUsage()
        self._usage_baseline: dict[str, int | None] | None = None

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
        self._usage.reset()
        self._usage_baseline = None

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
        if isinstance(note, CodexTokenUsageUpdated):
            return self._token_usage_updated(note)
        if isinstance(note, CodexTurnCompleted):
            return self._turn_completed(note)
        return []

    def on_approval_request(
        self,
        *,
        method: str,
        params: CodexApprovalParams,
        interaction_id: UUID,
    ) -> list[HarnessEvent]:
        if self._active_turn_id is None:
            raise DomainError(ErrorCode.INVALID_STATE, "approval without active turn")
        if isinstance(params, CodexCommandApprovalParams):
            argv = tuple(params.command or ())
            action = CommandApprovalAction(argv=argv) if argv else None
            return [
                InteractionRequestedPayload(
                    turn_id=self._active_turn_id,
                    interaction_id=interaction_id,
                    kind=InteractionKind.APPROVAL,
                    request=ApprovalRequestPayload(
                        tool_name="commandExecution",
                        command_args=argv or None,
                        summary=params.reason or "Codex command approval",
                        action=action,
                        available_decisions=(
                            ApprovalDecision.ALLOW_ONCE,
                            ApprovalDecision.ALLOW_SESSION,
                            ApprovalDecision.DENY,
                            ApprovalDecision.CANCEL,
                        ),
                    ),
                )
            ]
        first = (params.files or [None])[0]
        path = first.path if first is not None else None
        operation = _file_operation(first.kind if first is not None else None)
        action = (
            FileApprovalAction(path=path, operation=operation)
            if path is not None and operation is not None
            else None
        )
        return [
            InteractionRequestedPayload(
                turn_id=self._active_turn_id,
                interaction_id=interaction_id,
                kind=InteractionKind.APPROVAL,
                request=ApprovalRequestPayload(
                    tool_name="fileChange",
                    path=path,
                    operation=operation,
                    summary=params.reason or "Codex file change approval",
                    action=action,
                    available_decisions=(
                        ApprovalDecision.ALLOW_ONCE,
                        ApprovalDecision.DENY,
                        ApprovalDecision.CANCEL,
                    ),
                ),
            )
        ]

    def on_user_input_request(
        self,
        *,
        questions: tuple[CanonicalQuestion, ...],
        interaction_id: UUID,
    ) -> list[HarnessEvent]:
        if self._active_turn_id is None:
            raise DomainError(ErrorCode.INVALID_STATE, "user input without active turn")
        return [
            InteractionRequestedPayload(
                turn_id=self._active_turn_id,
                interaction_id=interaction_id,
                kind=InteractionKind.STRUCTURED_QUESTION,
                request=StructuredQuestionPayload(questions=questions),
            )
        ]

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
        # Items without a tool of their own are named as their approvals are,
        # never after the command line: consumers group and count by name.
        name = note.title or _ITEM_TOOL_NAMES.get(note.item_type, note.item_type)
        self._tool_names[note.item_id] = name
        command = self._redact(note.command) if note.command else ""
        return [
            ToolRequestedPayload(
                turn_id=self._active_turn_id,
                tool_id=tool_id,
                tool_name=name,
                arguments={"command": command} if command else {},
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

    def _token_usage_updated(self, note: CodexTokenUsageUpdated) -> list[HarnessEvent]:
        """Report the turn's totals so far from the thread's running totals.

        Codex counts per thread, not per turn: ``thread_total`` is what the
        thread has spent since it opened and ``usage`` what its last request
        spent. The turn's own total is the thread's growth since the turn
        began, so the reading taken before the turn's first request is the
        baseline every later reading is measured against. Reading a difference
        rather than summing the per-request figures keeps a dropped or replayed
        notification from moving the total.
        """
        if self._active_turn_id is None or self._resync_mode:
            return []
        if note.thread_total is None:
            # A host that reports only the last request's figures leaves the
            # turn's total to be added up from them.
            return list(self._usage.add(self._active_turn_id, **note.usage.model_dump()))
        totals = note.thread_total.model_dump()
        if self._usage_baseline is None:
            # The turn's first reading already includes its first request, so
            # the baseline is that reading less what the request spent.
            self._usage_baseline = _difference(totals, note.usage.model_dump())
        turn_totals = _difference(totals, self._usage_baseline)
        # The thread's totals are already cumulative, so each reading is the
        # turn's total so far and supersedes the reading before it; the turn
        # keeps reporting until it ends.
        return list(self._usage.replace(self._active_turn_id, final=False, **turn_totals))

    def _turn_completed(self, note: CodexTurnCompleted) -> list[HarnessEvent]:
        if self._active_turn_id is None:
            return []
        events: list[HarnessEvent] = []
        events.extend(self._close_open_streams())
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


def _file_operation(kind: str | None) -> FileOperation | None:
    if kind is None:
        return FileOperation.MODIFY
    normalized = kind.lower()
    if normalized in {"create", "add", "write"}:
        return FileOperation.CREATE
    if normalized in {"delete", "remove", "unlink"}:
        return FileOperation.DELETE
    if normalized in {"read", "view"}:
        return FileOperation.READ
    if normalized in {"edit", "modify", "update", "patch"}:
        return FileOperation.MODIFY
    return FileOperation.MODIFY

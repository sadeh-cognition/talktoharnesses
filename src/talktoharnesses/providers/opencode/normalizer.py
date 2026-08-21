"""OpenCode SSE/HTTP events → canonical HarnessEvent normalization."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast
from uuid import UUID, uuid5

from talktoharnesses.domain.enums import ApprovalDecision, ErrorCode, InteractionKind
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.domain.events import (
    AssistantMessageCompletedPayload,
    AssistantMessageDeltaPayload,
    AssistantMessageStartedPayload,
    HarnessEvent,
    InteractionRequestedPayload,
    TurnCompletedPayload,
    TurnFailedPayload,
    TurnInterruptedPayload,
    TurnOutcomeUnknownPayload,
    UsageUpdatedPayload,
)
from talktoharnesses.domain.models import (
    ApprovalRequestPayload,
    CanonicalQuestion,
    StructuredQuestionPayload,
)
from talktoharnesses.providers.opencode.schemas import OpenCodeStepFinishPart, parse_server_event

_NS = UUID("d0f6b2c4-5e7a-619c-bd3f-4a5b6c7d8e9f")


def _stable_uuid(native_key: str) -> UUID:
    return uuid5(_NS, native_key)


class OpenCodeNormalizer:
    def __init__(self) -> None:
        self._native_session_id: str | None = None
        self._active_turn_id: UUID | None = None
        self._resync_mode = False
        self._message_id: UUID | None = None
        self._message_text = ""
        self._message_seq = 0
        self._has_assistant_message = False
        self._seen_native_ids: set[str] = set()
        self._seen_offsets: set[str] = set()
        self._redaction_patterns: tuple[str, ...] = ()
        self._child_sessions: set[str] = set()
        self._turn_child_sessions: set[str] = set()
        self._root_message_id: str | None = None
        self._usage_steps = 0
        self._usage_all_have_total = True
        self._usage: dict[str, int] = {}

    def set_redaction_patterns(self, patterns: Sequence[str]) -> None:
        self._redaction_patterns = tuple(sorted((p for p in patterns if p), key=len, reverse=True))

    def set_session(self, native_session_id: str, *, resync: bool = False) -> None:
        self._native_session_id = native_session_id
        self._resync_mode = resync

    def begin_turn(self, turn_id: UUID, *, root_message_id: str | None = None) -> None:
        self._active_turn_id = turn_id
        self._message_id = None
        self._message_text = ""
        self._message_seq = 0
        self._has_assistant_message = False
        self._turn_child_sessions.clear()
        self._root_message_id = root_message_id
        self._usage_steps = 0
        self._usage_all_have_total = True
        self._usage.clear()

    def import_seen(
        self,
        native_ids: frozenset[str],
        stream_offsets: frozenset[str],
    ) -> None:
        self._seen_native_ids.update(native_ids)
        self._seen_offsets.update(stream_offsets)

    def export_seen(self) -> tuple[frozenset[str], frozenset[str]]:
        return frozenset(self._seen_native_ids), frozenset(self._seen_offsets)

    def on_server_event(self, raw: dict[str, Any]) -> list[HarnessEvent]:
        event = parse_server_event(raw)
        props = event.properties
        # Child discovery must run before session filtering so parentID events
        # whose sessionID is the new child are not dropped as foreign.
        session_info = _mapping(props.get("info"))
        parent_id = props.get("parentID") or session_info.get("parentID")
        if event.type.startswith("session.") and parent_id == self._native_session_id:
            child = props.get("sessionID") or session_info.get("id")
            if isinstance(child, str):
                self._child_sessions.add(child)
                if self._active_turn_id is not None:
                    self._turn_child_sessions.add(child)
            return []
        session_id = props.get("sessionID") or props.get("session_id")
        if isinstance(session_id, str) and not self.accepts_session(session_id):
            return []
        if event.type in {"session.status", "session.idle"} and (
            not isinstance(session_id, str) or not self.accepts_terminal_session(session_id)
        ):
            return []
        if event.type in {
            "server.connected",
            "server.heartbeat",
            "permission.asked",
            "permission.replied",
            "question.asked",
            "question.replied",
            "question.rejected",
        }:
            return []
        if event.type == "message.part.delta":
            return self._part_delta(props)
        if event.type == "message.part.updated":
            return self._part_updated(props)
        if event.type == "session.status":
            return self._session_status(props)
        if event.type == "session.idle":
            return self._session_status({**props, "status": props.get("status") or "idle"})
        # Unknown event types fail the runtime.
        if event.type not in {
            "message.updated",
            "session.updated",
            "session.diff",
            "todo.updated",
        }:
            raise DomainError(
                ErrorCode.UNSUPPORTED_NATIVE_EVENT,
                f"unsupported opencode event type: {event.type}",
            )
        return []

    def accepts_session(self, session_id: str) -> bool:
        return session_id == self._native_session_id or session_id in self._child_sessions

    def accepts_terminal_session(self, session_id: str) -> bool:
        return session_id == self._native_session_id

    def usage_history_requests(self) -> tuple[tuple[str, str | None], ...]:
        if self._native_session_id is None:
            return ()
        children = tuple((session_id, None) for session_id in sorted(self._turn_child_sessions))
        return ((self._native_session_id, self._root_message_id), *children)

    def on_message_history(
        self,
        session_id: str,
        messages: list[object],
        *,
        after_message_id: str | None,
    ) -> None:
        include = after_message_id is None
        for item in messages:
            message = _mapping(item)
            info = _mapping(message.get("info"))
            if not include and info.get("id") == after_message_id:
                include = True
            if not include:
                continue
            parts = message.get("parts")
            if not isinstance(parts, list):
                continue
            for part_obj in cast(list[object], parts):
                part = _mapping(part_obj)
                if part.get("type") == "step-finish":
                    self._record_usage(OpenCodeStepFinishPart.model_validate(part), session_id)

    def on_permission(
        self,
        *,
        permission_id: str,
        tool: str | None,
        title: str | None,
        interaction_id: UUID,
    ) -> list[HarnessEvent]:
        if self._active_turn_id is None:
            raise DomainError(ErrorCode.INVALID_STATE, "permission without active turn")
        del permission_id
        return [
            InteractionRequestedPayload(
                turn_id=self._active_turn_id,
                interaction_id=interaction_id,
                kind=InteractionKind.APPROVAL,
                request=ApprovalRequestPayload(
                    tool_name=tool,
                    summary=title or tool,
                    available_decisions=(
                        ApprovalDecision.ALLOW_ONCE,
                        ApprovalDecision.DENY,
                        ApprovalDecision.CANCEL,
                    ),
                ),
            )
        ]

    def on_question(
        self,
        *,
        question_id: str,
        questions: tuple[CanonicalQuestion, ...],
        interaction_id: UUID,
    ) -> list[HarnessEvent]:
        if self._active_turn_id is None:
            raise DomainError(ErrorCode.INVALID_STATE, "question without active turn")
        del question_id
        return [
            InteractionRequestedPayload(
                turn_id=self._active_turn_id,
                interaction_id=interaction_id,
                kind=InteractionKind.STRUCTURED_QUESTION,
                request=StructuredQuestionPayload(questions=questions),
            )
        ]

    def on_outcome_unknown(self, message: str) -> list[HarnessEvent]:
        if self._active_turn_id is None:
            return []
        events: list[HarnessEvent] = [
            TurnOutcomeUnknownPayload(
                turn_id=self._active_turn_id,
                message=message,
            )
        ]
        self._active_turn_id = None
        return events

    def _part_delta(self, props: dict[str, Any]) -> list[HarnessEvent]:
        if self._active_turn_id is None or self._resync_mode:
            return []
        message_id = str(props.get("messageID") or "")
        part_id = str(props.get("partID") or "")
        field = str(props.get("field") or "")
        delta = str(props.get("delta") or "")
        if field not in {"text", "content"} or not delta:
            return []
        key = f"{message_id}:{part_id}:{self._message_seq + 1}"
        if key in self._seen_offsets:
            return []
        events: list[HarnessEvent] = []
        if self._message_id is None:
            self._message_id = _stable_uuid(f"msg:{message_id or part_id}")
            self._has_assistant_message = True
            events.append(
                AssistantMessageStartedPayload(
                    turn_id=self._active_turn_id,
                    message_id=self._message_id,
                )
            )
        text = self._redact(delta)
        self._message_seq += 1
        self._message_text += text
        events.append(
            AssistantMessageDeltaPayload(
                turn_id=self._active_turn_id,
                message_id=self._message_id,
                sequence=self._message_seq,
                text=text,
            )
        )
        self._seen_offsets.add(key)
        return events

    def _part_updated(self, props: dict[str, Any]) -> list[HarnessEvent]:
        part = _mapping(props.get("part"))
        if part.get("type") != "step-finish":
            return []
        parsed = OpenCodeStepFinishPart.model_validate(part)
        self._record_usage(parsed, parsed.sessionID)
        return []

    def _record_usage(self, part: OpenCodeStepFinishPart, session_id: str) -> None:
        if (
            self._active_turn_id is None
            or self._resync_mode
            or not self.accepts_session(session_id)
        ):
            return
        native_key = f"opencode-step:{session_id}:{part.id}"
        if native_key in self._seen_native_ids:
            return
        self._seen_native_ids.add(native_key)
        self._usage_steps += 1
        self._usage["input_tokens"] = self._usage.get("input_tokens", 0) + part.tokens.input
        self._usage["output_tokens"] = self._usage.get("output_tokens", 0) + part.tokens.output
        self._usage["cached_input_tokens"] = (
            self._usage.get("cached_input_tokens", 0) + part.tokens.cache.read
        )
        if part.tokens.total is None:
            self._usage_all_have_total = False
        else:
            self._usage["total_tokens"] = self._usage.get("total_tokens", 0) + part.tokens.total

    def _session_status(self, props: dict[str, Any]) -> list[HarnessEvent]:
        if self._active_turn_id is None:
            return []
        status_obj = props.get("status")
        if isinstance(status_obj, dict):
            typed = cast(dict[str, object], status_obj)
            status = str(typed.get("type") or "").lower()
        else:
            status = str(status_obj or "").lower()
        events: list[HarnessEvent] = []
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
        if status in {"idle", "completed", "done"}:
            if self._usage_steps:
                usage = dict(self._usage)
                if not self._usage_all_have_total:
                    usage.pop("total_tokens", None)
                events.append(
                    UsageUpdatedPayload(
                        turn_id=self._active_turn_id,
                        input_tokens=usage.get("input_tokens"),
                        output_tokens=usage.get("output_tokens"),
                        total_tokens=usage.get("total_tokens"),
                        cached_input_tokens=usage.get("cached_input_tokens"),
                    )
                )
            events.append(
                TurnCompletedPayload(
                    turn_id=self._active_turn_id,
                    terminal_reason=status,
                    has_assistant_message=self._has_assistant_message,
                )
            )
            self._active_turn_id = None
        elif status in {"aborted", "interrupted", "cancelled"}:
            events.append(
                TurnInterruptedPayload(
                    turn_id=self._active_turn_id,
                    reason=status,
                )
            )
            self._active_turn_id = None
        elif status in {"error", "failed"}:
            events.append(
                TurnFailedPayload(
                    turn_id=self._active_turn_id,
                    error_code="provider_error",
                    message="opencode session status failed",
                )
            )
            self._active_turn_id = None
        return events

    def _redact(self, text: str) -> str:
        out = text
        for pattern in self._redaction_patterns:
            if pattern:
                out = out.replace(pattern, "***")
        return out


def _mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in cast(dict[object, object], value).items()}

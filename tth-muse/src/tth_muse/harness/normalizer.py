"""Normalize MSP item streams and turn-scoped token accounting."""

from __future__ import annotations

from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from tth_types import events as ev
from tth_types.enums import ToolOutcome

from tth_muse.shared.redaction import StreamingTextRedactor


def _dict(value: object) -> dict[str, Any]:
    """MSP serializes absent optionals as ``null``; treat those as empty."""
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _tool_name(item: dict[str, Any]) -> str:
    # Official transcripts carry the name under ``tool``; older drafts used
    # ``toolName``/``name``.
    for key in ("tool", "toolName", "name"):
        value = item.get(key)
        if isinstance(value, str) and value:
            return value
    return "tool"


class MuseNormalizer:
    def __init__(self) -> None:
        self.turn_id: UUID | None = None
        self.native_turn_id: str | None = None
        self.turn_started = False
        self.session_id: str | None = None
        self.patterns: tuple[str, ...] = ()
        self._items: dict[str, dict[str, Any]] = {}
        self._sequences: dict[str, int] = {}
        self._redactors: dict[str, StreamingTextRedactor] = {}
        self._seen: set[str] = set()
        self._usage: dict[str, Any] = {}
        self._has_message = False

    def redact(self, text: str) -> str:
        for pattern in self.patterns:
            text = text.replace(pattern, "[REDACTED]")
        return text

    def import_seen(self, native_ids: frozenset[str], offsets: frozenset[str]) -> None:
        self._seen.update(offsets)

    def export_seen(self) -> tuple[frozenset[str], frozenset[str]]:
        return frozenset(), frozenset(self._seen)

    def begin_turn(self, turn_id: UUID) -> None:
        self.turn_id = turn_id
        self.native_turn_id = None
        self.turn_started = False
        self._items.clear()
        self._sequences.clear()
        self._redactors.clear()
        self._usage.clear()
        self._has_message = False

    def disconnected(self, message: str) -> list[ev.HarnessEvent]:
        if self.turn_id is None:
            return []
        event = ev.TurnOutcomeUnknownPayload(turn_id=self.turn_id, message=message)
        self.turn_id = None
        return [event]

    def on_notification(self, method: str, params: dict[str, Any]) -> list[ev.HarnessEvent]:
        if self.turn_id is None or params.get("sessionId") != self.session_id:
            return []
        cursor = params.get("viewCursor")
        key = f"{self.session_id}:{cursor}:{method}" if cursor else None
        if key is not None:
            if key in self._seen:
                return []
            self._seen.add(key)
        if params.get("turnId") and self.native_turn_id and params["turnId"] != self.native_turn_id:
            return []
        if method == "turn/started":
            self.turn_started = True
        if method == "turn/unqueued":
            event = ev.TurnInterruptedPayload(turn_id=self.turn_id)
            self.turn_id = None
            return [event]
        if method == "session/tokenUsage":
            usage = _dict(params.get("usage"))
            values = {
                "input_tokens": params.get("promptTokens"),
                "output_tokens": usage.get("outputTokens"),
                "total_tokens": params.get("totalTokens"),
                "cached_input_tokens": usage.get("cacheReadTokens", usage.get("cachedTokens")),
            }
            for name, value in values.items():
                if type(value) is int and value >= 0:
                    self._usage[name] = self._usage.get(name, 0) + value
            if self._usage:
                return [ev.UsageUpdatedPayload(turn_id=self.turn_id, **self._usage)]
            return []
        if method == "turn/completed":
            result: list[ev.HarnessEvent] = []
            # Older hosts may only report the aggregate on the terminal frame.
            if not self._usage and _dict(params.get("usage")):
                usage = _dict(params.get("usage"))
                values = {
                    target: usage[source]
                    for source, target in {
                        "inputTokens": "input_tokens",
                        "outputTokens": "output_tokens",
                        "cachedTokens": "cached_input_tokens",
                    }.items()
                    if type(usage.get(source)) is int and usage[source] >= 0
                }
                if values:
                    result.append(ev.UsageUpdatedPayload(turn_id=self.turn_id, **values))
            terminal = params.get("terminal")
            if terminal == "completed":
                result.append(
                    ev.TurnCompletedPayload(
                        turn_id=self.turn_id, has_assistant_message=self._has_message
                    )
                )
            elif terminal == "cancelled":
                result.append(ev.TurnInterruptedPayload(turn_id=self.turn_id))
            elif terminal == "failed":
                result.append(
                    ev.TurnFailedPayload(
                        turn_id=self.turn_id,
                        error_code="provider_error",
                        message=self.redact(
                            str(
                                _dict(params.get("error")).get("message")
                                or params.get("reason")
                                or "Muse turn failed"
                            )
                        ),
                    )
                )
            else:
                result.append(
                    ev.TurnOutcomeUnknownPayload(
                        turn_id=self.turn_id, message="Unknown Muse terminal state"
                    )
                )
            self.turn_id = None
            return result
        if method in {"item/started", "item/updated", "item/completed"}:
            return self._item(method, _dict(params.get("item")))
        if method == "item/delta":
            item_id = params["itemId"]
            item = self._items.get(item_id)
            field = str(params.get("field", ""))
            if item is None or (
                field not in {"text", "output"} and not field.startswith("summary.")
            ):
                return []
            text = self._redactors[item_id].feed(params["delta"])
            return self._delta(item, text) if text else []
        return []

    def _id(self, item: dict[str, Any]) -> UUID:
        return uuid5(NAMESPACE_URL, f"muse:{self.session_id}:{item['itemId']}")

    def _delta(self, item: dict[str, Any], text: str) -> list[ev.HarnessEvent]:
        assert self.turn_id is not None
        identity = self._id(item)
        sequence = self._sequences.get(item["itemId"], 0) + 1
        self._sequences[item["itemId"]] = sequence
        if item["kind"] == "agentMessage":
            return [
                ev.AssistantMessageDeltaPayload(
                    turn_id=self.turn_id, message_id=identity, sequence=sequence, text=text
                )
            ]
        if item["kind"] == "reasoning":
            return [
                ev.ReasoningDeltaPayload(turn_id=self.turn_id, reasoning_id=identity, text=text)
            ]
        if item["kind"] == "toolCall":
            return [
                ev.ToolOutputDeltaPayload(
                    turn_id=self.turn_id, tool_id=identity, sequence=sequence, text=text
                )
            ]
        return []

    def _item(self, method: str, item: dict[str, Any]) -> list[ev.HarnessEvent]:
        assert self.turn_id is not None
        if self.native_turn_id and item.get("turnId") != self.native_turn_id:
            return []
        kind = item.get("kind")
        item_id = item.get("itemId")
        if kind not in {"agentMessage", "reasoning", "toolCall"} or not isinstance(item_id, str):
            return []
        identity = self._id(item)
        result: list[ev.HarnessEvent] = []
        if item_id not in self._items:
            self._redactors[item_id] = StreamingTextRedactor(self.patterns)
            if kind == "agentMessage":
                result.append(
                    ev.AssistantMessageStartedPayload(turn_id=self.turn_id, message_id=identity)
                )
            elif kind == "reasoning":
                result.append(
                    ev.ReasoningStartedPayload(turn_id=self.turn_id, reasoning_id=identity)
                )
            else:
                name = _tool_name(item)
                result.extend(
                    [
                        ev.ToolRequestedPayload(
                            turn_id=self.turn_id,
                            tool_id=identity,
                            tool_name=name,
                            arguments={"raw": self.redact(str(item.get("args") or ""))},
                        ),
                        ev.ToolStartedPayload(
                            turn_id=self.turn_id, tool_id=identity, tool_name=name
                        ),
                    ]
                )
        self._items[item_id] = item
        if method != "item/completed":
            return result
        tail = self._redactors[item_id].flush()
        if tail:
            result.extend(self._delta(item, tail))
        summary = item.get("summary")
        lines = cast(list[object], summary) if isinstance(summary, list) else []
        text = self.redact(str(item.get("text") or "") or "\n".join(str(line) for line in lines))
        if kind == "agentMessage":
            self._has_message = self._has_message or bool(text)
            result.append(
                ev.AssistantMessageCompletedPayload(
                    turn_id=self.turn_id, message_id=identity, text=text
                )
            )
        elif kind == "reasoning":
            result.append(
                ev.ReasoningCompletedPayload(turn_id=self.turn_id, reasoning_id=identity, text=text)
            )
        else:
            result.append(
                ev.ToolCompletedPayload(
                    turn_id=self.turn_id,
                    tool_id=identity,
                    tool_name=_tool_name(item),
                    outcome={
                        "completed": ToolOutcome.SUCCESS,
                        "failed": ToolOutcome.FAILURE,
                        "cancelled": ToolOutcome.CANCELLED,
                    }.get(str(item.get("status")), ToolOutcome.UNKNOWN),
                    output_tail=self.redact(str(item.get("visibleOutput") or "")),
                )
            )
        return result

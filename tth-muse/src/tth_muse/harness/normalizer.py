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


_ITEM_METHODS = frozenset({"item/started", "item/updated", "item/completed"})
_ITEM_KINDS = frozenset({"agentMessage", "reasoning", "toolCall"})
_TERMINAL_ITEM_STATUSES = frozenset({"completed", "failed", "cancelled"})


def _source_sequence(params: dict[str, Any]) -> int | None:
    """The durable record sequence a view event folded from, when present."""
    last = _dict(_dict(params.get("sourceRange")).get("last"))
    sequence = last.get("sequence")
    return sequence if type(sequence) is int else None


def is_provisional(method: str, params: dict[str, Any]) -> bool:
    """A paged view event that only reflects an unfinished run.

    ``view/page`` folds the session at read time and renders an in-flight
    turn as failed/incomplete: the running tool item comes back as
    ``item/completed`` with ``status: failed, reason: incomplete`` and the
    turn as ``turn/completed`` with ``terminal: failed, reason: incomplete``
    and no ``durationMs``. Neither is a durable fact; push delivery never
    carries them and a replay must not either.
    """
    if method == "turn/completed":
        return "durationMs" not in params or params.get("reason") == "incomplete"
    if method in _ITEM_METHODS:
        item = _dict(params.get("item"))
        return item.get("reason") == "incomplete" and str(item.get("status")) == "failed"
    return False


class MuseNormalizer:
    def __init__(self) -> None:
        self.turn_id: UUID | None = None
        self.native_turn_id: str | None = None
        self.turn_started = False
        self.session_id: str | None = None
        self.patterns: tuple[str, ...] = ()
        # Last push cursor observed for this session: the anchor for
        # re-subscribing after the host's push delivery dies.
        self.last_view_cursor: str | None = None
        self._items: dict[str, dict[str, Any]] = {}
        self._sequences: dict[str, int] = {}
        self._redactors: dict[str, StreamingTextRedactor] = {}
        self._seen: set[str] = set()
        self._usage: dict[str, Any] = {}
        self._usage_sequence = -1
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
        self._usage_sequence = -1
        self._has_message = False

    def classify(self, method: str, params: dict[str, Any]) -> str:
        """How a paged view event relates to what push delivery forwarded.

        ``"new"``: carries something not yet forwarded; ``"known"``: already
        forwarded, so anything older in the page is too; ``"irrelevant"``:
        never forwarded by design (other sessions or turns, item kinds with
        no TTH mapping, provisional folds of the unfinished run) and says
        nothing about delivery either way. Identity, not view cursor: paged
        reads number their cursors differently from push delivery.
        """
        if self.turn_id is None or params.get("sessionId") != self.session_id:
            return "irrelevant"
        if is_provisional(method, params):
            return "irrelevant"
        if method in _ITEM_METHODS:
            item = _dict(params.get("item"))
            if self.native_turn_id and item.get("turnId") != self.native_turn_id:
                return "irrelevant"
            item_id = item.get("itemId")
            if item.get("kind") not in _ITEM_KINDS or not isinstance(item_id, str):
                return "irrelevant"
            stored = self._items.get(item_id)
            if stored is None:
                return "new"
            if method == "item/completed" and not self._completed(stored):
                return "new"
            return "known"
        if method == "session/tokenUsage":
            sequence = _source_sequence(params)
            if sequence is None:
                return "irrelevant"
            return "new" if sequence > self._usage_sequence else "known"
        if method == "turn/completed":
            if not self.native_turn_id or params.get("turnId") != self.native_turn_id:
                return "irrelevant"
            return "new"
        return "irrelevant"

    def is_new(self, method: str, params: dict[str, Any]) -> bool:
        return self.classify(method, params) == "new"

    @staticmethod
    def _completed(item: dict[str, Any]) -> bool:
        return str(item.get("status")) in _TERMINAL_ITEM_STATUSES

    def disconnected(self, message: str) -> list[ev.HarnessEvent]:
        if self.turn_id is None:
            return []
        event = ev.TurnOutcomeUnknownPayload(turn_id=self.turn_id, message=message)
        self.turn_id = None
        return [event]

    def drop_reason(self, params: dict[str, Any]) -> str:
        """Why ``on_notification`` would ignore a frame with these params."""
        if self.turn_id is None:
            return "no_active_turn"
        if params.get("sessionId") != self.session_id:
            return "other_session"
        if params.get("turnId") and self.native_turn_id and params["turnId"] != self.native_turn_id:
            return "other_turn"
        # Either a method with no TTH mapping or a viewCursor replay that was
        # already forwarded.
        return "unmapped_or_duplicate"

    def on_notification(self, method: str, params: dict[str, Any]) -> list[ev.HarnessEvent]:
        cursor = params.get("viewCursor")
        if isinstance(cursor, str) and cursor and params.get("sessionId") == self.session_id:
            self.last_view_cursor = cursor
        if self.turn_id is None or params.get("sessionId") != self.session_id:
            return []
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
            # Replayed after a push recovery, the same durable fact must not
            # be added to the turn's cumulative usage twice.
            sequence = _source_sequence(params)
            if sequence is not None:
                if sequence <= self._usage_sequence:
                    return []
                self._usage_sequence = sequence
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
        stored = self._items.get(item_id)
        if stored is not None and self._completed(stored):
            # Already forwarded as complete (a paged replay of a pushed item).
            return []
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

"""Map ACP ``session/update`` payloads to canonical RuntimeEvents."""

from __future__ import annotations

from typing import Any

from talktoharnesses.events import (
    ContentDelta,
    ItemCompleted,
    ItemStarted,
    ItemUpdated,
    RuntimeEvent,
    RuntimeWarning,
    ThreadTokenUsageUpdated,
    TurnPlanUpdated,
)
from talktoharnesses.types import PlanStep, TokenUsage


def _content_text(content: Any) -> str | None:
    if content is None:
        return None
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        return str(content.get("text") or "") if content.get("text") is not None else None
    text = getattr(content, "text", None)
    if text is not None:
        return str(text)
    return None


def _update_kind(update: Any) -> str | None:
    if isinstance(update, dict):
        kind = update.get("sessionUpdate") or update.get("session_update")
        return str(kind) if kind is not None else None
    kind = getattr(update, "session_update", None) or getattr(update, "sessionUpdate", None)
    return str(kind) if kind is not None else None


def _dump(update: Any) -> dict[str, Any]:
    if isinstance(update, dict):
        return update
    if hasattr(update, "model_dump"):
        return update.model_dump(by_alias=True, mode="json")  # type: ignore[no-any-return]
    return {"repr": repr(update)}


def normalize_session_update(
    *,
    provider: str,
    session_id: str,
    thread_id: str | None,
    turn_id: str | None,
    update: Any,
) -> list[RuntimeEvent]:
    """Convert one ACP session update into zero or more canonical events."""
    kind = _update_kind(update)
    raw = _dump(update)
    tid = thread_id or session_id
    events: list[RuntimeEvent] = []

    if kind == "agent_message_chunk":
        content = raw.get("content")
        text = _content_text(content)
        if text is not None:
            events.append(
                ContentDelta(
                    provider=provider,
                    thread_id=tid,
                    turn_id=turn_id,
                    text=text,
                    content_kind="text",
                    item_id=_str_or_none(raw.get("messageId") or raw.get("message_id")),
                    raw=raw,
                )
            )
        return events

    if kind == "agent_thought_chunk":
        content = raw.get("content")
        text = _content_text(content)
        if text is not None:
            events.append(
                ContentDelta(
                    provider=provider,
                    thread_id=tid,
                    turn_id=turn_id,
                    text=text,
                    content_kind="reasoning",
                    item_id=_str_or_none(raw.get("messageId") or raw.get("message_id")),
                    raw=raw,
                )
            )
        return events

    if kind == "tool_call":
        tool_call_id = raw.get("toolCallId") or raw.get("tool_call_id") or raw.get("id")
        title = raw.get("title") or raw.get("kind") or raw.get("name")
        events.append(
            ItemStarted(
                provider=provider,
                thread_id=tid,
                turn_id=turn_id,
                item_id=_str_or_none(tool_call_id),
                item_type="dynamic_tool_call",
                title=str(title) if title else None,
                raw=raw,
            )
        )
        return events

    if kind == "tool_call_update":
        tool_call_id = raw.get("toolCallId") or raw.get("tool_call_id") or raw.get("id")
        status = raw.get("status")
        events.append(
            ItemUpdated(
                provider=provider,
                thread_id=tid,
                turn_id=turn_id,
                item_id=_str_or_none(tool_call_id),
                item_type="dynamic_tool_call",
                status=str(status) if status else None,
                raw=raw,
            )
        )
        if status in ("completed", "failed", "cancelled"):
            events.append(
                ItemCompleted(
                    provider=provider,
                    thread_id=tid,
                    turn_id=turn_id,
                    item_id=_str_or_none(tool_call_id),
                    item_type="dynamic_tool_call",
                    status=str(status),
                    raw=raw,
                )
            )
        return events

    if kind in ("plan", "agent_plan_update"):
        entries = raw.get("entries") or raw.get("steps") or []
        steps: list[PlanStep] = []
        if isinstance(entries, list):
            for i, e in enumerate(entries):
                if not isinstance(e, dict):
                    continue
                steps.append(
                    PlanStep(
                        step_id=str(e.get("id") or i),
                        title=str(e.get("content") or e.get("title") or ""),
                        status=str(e["status"]) if e.get("status") else None,
                    )
                )
        events.append(
            TurnPlanUpdated(
                provider=provider,
                thread_id=tid,
                turn_id=turn_id,
                steps=steps,
                raw=raw,
            )
        )
        return events

    if kind == "usage_update":
        usage_raw = raw.get("usage")
        usage = usage_raw if isinstance(usage_raw, dict) else raw
        events.append(
            ThreadTokenUsageUpdated(
                provider=provider,
                thread_id=tid,
                turn_id=turn_id,
                usage=TokenUsage(
                    input_tokens=_int(usage.get("inputTokens") or usage.get("input_tokens")),
                    output_tokens=_int(usage.get("outputTokens") or usage.get("output_tokens")),
                    total_tokens=_int(usage.get("totalTokens") or usage.get("total_tokens")),
                ),
                raw=raw,
            )
        )
        return events

    events.append(
        RuntimeWarning(
            provider=provider,
            thread_id=tid,
            turn_id=turn_id,
            message=f"unhandled ACP session update: {kind}",
            code="unhandled_acp_update",
            raw=raw,
        )
    )
    return events


def _int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)

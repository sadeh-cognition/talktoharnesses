"""Event serialization round-trip and discriminated-union dispatch."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from pydantic import TypeAdapter, ValidationError

from talktoharnesses.events import (
    ContentDelta,
    ItemStarted,
    RequestOpened,
    RuntimeErrorEvent,
    RuntimeEvent,
    RuntimeWarning,
    SessionStarted,
    TurnAborted,
    TurnCompleted,
    TurnStarted,
    parse_runtime_event,
    runtime_event_to_dict,
)

ADAPTER = TypeAdapter(RuntimeEvent)

PROVIDER = "test"


def _base(**extra: object) -> dict[str, object]:
    data: dict[str, object] = {
        "provider": PROVIDER,
        "thread_id": "thr-1",
        "created_at": datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
    }
    data.update(extra)
    return data


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "event",
    [
        SessionStarted(provider=PROVIDER, session_id="s1", model="gpt-test"),
        TurnStarted(provider=PROVIDER, turn_id="t1", thread_id="thr-1"),
        ContentDelta(provider=PROVIDER, turn_id="t1", text="hello", content_kind="text"),
        TurnCompleted(provider=PROVIDER, turn_id="t1", stop_reason="end_turn"),
        TurnAborted(provider=PROVIDER, turn_id="t1", reason="user"),
        ItemStarted(
            provider=PROVIDER,
            turn_id="t1",
            item_id="i1",
            item_type="assistant_message",
            title="reply",
        ),
        RequestOpened(
            provider=PROVIDER,
            request_id="r1",
            request_type="command_execution",
            title="run ls",
            detail="ls -la",
        ),
        RuntimeWarning(provider=PROVIDER, message="unknown event family", code="deferred"),
        RuntimeErrorEvent(provider=PROVIDER, message="boom", code="E1"),
    ],
)
def test_event_round_trip_dict(event: RuntimeEvent) -> None:
    dumped = runtime_event_to_dict(event)
    restored = parse_runtime_event(dumped)
    assert type(restored) is type(event)
    assert restored.model_dump(mode="json") == dumped  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "event",
    [
        ContentDelta(provider=PROVIDER, text="stream me", turn_id="t1"),
        SessionStarted(provider=PROVIDER, session_id="s-json"),
    ],
)
def test_event_round_trip_json(event: RuntimeEvent) -> None:
    payload = json.dumps(runtime_event_to_dict(event))
    restored = parse_runtime_event(payload)
    assert type(restored) is type(event)
    assert restored.model_dump(mode="json") == json.loads(payload)  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Discriminated-union dispatch
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("type_name", "extra", "cls"),
    [
        ("session.started", {"session_id": "s1"}, SessionStarted),
        ("turn.started", {"turn_id": "t1"}, TurnStarted),
        ("turn.completed", {"turn_id": "t1"}, TurnCompleted),
        ("turn.aborted", {"turn_id": "t1"}, TurnAborted),
        ("content.delta", {"text": "hi"}, ContentDelta),
        (
            "item.started",
            {"item_type": "reasoning", "item_id": "i1"},
            ItemStarted,
        ),
        (
            "request.opened",
            {"request_id": "r1", "request_type": "file_change"},
            RequestOpened,
        ),
        ("runtime.warning", {"message": "x"}, RuntimeWarning),
        ("runtime.error", {"message": "y"}, RuntimeErrorEvent),
    ],
)
def test_discriminated_union_dispatch(
    type_name: str,
    extra: dict[str, object],
    cls: type,
) -> None:
    data = _base(type=type_name, **extra)
    event = ADAPTER.validate_python(data)
    assert isinstance(event, cls)
    assert event.type == type_name


def test_unknown_type_rejected() -> None:
    data = _base(type="thread.realtime.unknown", message="nope")
    with pytest.raises(ValidationError):
        ADAPTER.validate_python(data)


def test_missing_required_field_rejected() -> None:
    # content.delta requires text
    data = _base(type="content.delta")
    with pytest.raises(ValidationError):
        ADAPTER.validate_python(data)


def test_raw_passthrough_preserved() -> None:
    raw = {"provider_specific": True, "nested": {"a": 1}}
    event = ContentDelta(provider=PROVIDER, text="x", raw=raw)
    restored = parse_runtime_event(runtime_event_to_dict(event))
    assert isinstance(restored, ContentDelta)
    assert restored.raw == raw


def test_event_id_auto_generated_and_stable_on_dump() -> None:
    e1 = TurnStarted(provider=PROVIDER, turn_id="t1")
    e2 = TurnStarted(provider=PROVIDER, turn_id="t1")
    assert e1.event_id
    assert e1.event_id != e2.event_id
    dumped = runtime_event_to_dict(e1)
    restored = parse_runtime_event(dumped)
    assert restored.event_id == e1.event_id  # type: ignore[union-attr]

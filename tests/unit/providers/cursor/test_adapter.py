"""Cursor adapter lifecycle tests."""

from __future__ import annotations

from uuid import uuid4

import pytest

from talktoharnesses.domain.events import TurnOutcomeUnknownPayload
from talktoharnesses.providers.cursor.adapter import CursorAdapter


@pytest.mark.asyncio
async def test_prompt_protocol_failure_publishes_unknown_before_stream_close() -> None:
    adapter = CursorAdapter()
    turn_id = uuid4()
    adapter._normalizer.set_session("cursor-session")  # pyright: ignore[reportPrivateUsage]
    adapter._normalizer.begin_turn(turn_id)  # pyright: ignore[reportPrivateUsage]

    await adapter._emit_prompt_outcome_unknown_and_close(  # pyright: ignore[reportPrivateUsage]
        "connection closed"
    )

    event = await adapter._event_q.get()  # pyright: ignore[reportPrivateUsage]
    end = await adapter._event_q.get()  # pyright: ignore[reportPrivateUsage]
    assert isinstance(event, TurnOutcomeUnknownPayload)
    assert event.turn_id == turn_id
    assert event.delivery_phase == "delivered"
    assert end is None

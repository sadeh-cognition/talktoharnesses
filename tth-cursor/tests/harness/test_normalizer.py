"""Cursor/ACP normalizer smoke — shared AcpSessionNormalizer."""

from __future__ import annotations

from uuid import uuid4

from tth_types.events import AssistantMessageDeltaPayload, TurnCompletedPayload

from tth_cursor.harness.normalizer import CursorNormalizer


def test_message_chunk_and_terminal_without_final_required() -> None:
    normalizer = CursorNormalizer()
    normalizer.set_session("sess-1")
    turn_id = uuid4()
    normalizer.begin_turn(turn_id)
    events = normalizer.on_session_update(
        {
            "sessionId": "sess-1",
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": "hi",
                "messageId": "m1",
            },
        }
    )
    assert any(isinstance(e, AssistantMessageDeltaPayload) for e in events)
    terminal = normalizer.on_prompt_terminal("end_turn")
    assert any(isinstance(e, TurnCompletedPayload) for e in terminal)

"""Hypothesis: queued prompt coalescing and edit invariants."""

from __future__ import annotations

from datetime import UTC, datetime

from hypothesis import given, settings
from hypothesis import strategies as st

from talktoharnesses.domain import (
    cancel_queued_prompt,
    edit_queued_prompt,
    new_conversation_state,
    start_turn,
    submit_turn,
)


@settings(max_examples=40)
@given(st.lists(st.text(min_size=1, max_size=30), min_size=2, max_size=6))
def test_coalesce_preserves_order(prompts: list[str]) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    state = new_conversation_state(owner_id="o", now=now)
    r = submit_turn(state, prompt="root", idempotency_key="root", now=now)
    r = start_turn(r.state, now=now)
    state = r.state
    for i, p in enumerate(prompts):
        r = submit_turn(state, prompt=p, idempotency_key=f"k{i}", now=now)
        state = r.state
    assert state.queued_user_text == "\n".join(prompts)


@settings(max_examples=20)
@given(st.text(min_size=1, max_size=40))
def test_edit_replaces_entire_queue(text: str) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    state = new_conversation_state(owner_id="o", now=now)
    r = submit_turn(state, prompt="root", idempotency_key="root", now=now)
    r = start_turn(r.state, now=now)
    r = submit_turn(r.state, prompt="a", idempotency_key="a", now=now)
    r = submit_turn(r.state, prompt="b", idempotency_key="b", now=now)
    r = edit_queued_prompt(r.state, prompt=text, now=now)
    assert r.state.queued_user_text == text
    r = cancel_queued_prompt(r.state, now=now)
    assert r.state.queued_turn is None

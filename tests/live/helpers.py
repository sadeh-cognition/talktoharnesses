"""Shared helpers for opt-in live create/resume/interaction gates."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

from talktoharnesses import __version__
from talktoharnesses.domain.enums import ApprovalDecision
from talktoharnesses.domain.events import InteractionRequestedPayload
from talktoharnesses.domain.models import (
    HarnessCapabilities,
    InteractionAnswer,
    LaunchSnapshot,
)
from talktoharnesses.providers.adapter import HarnessInteractionRequest, HarnessSession

TERMINAL_TYPES = frozenset(
    {"turn_completed", "turn_failed", "turn_interrupted", "turn_outcome_unknown"}
)


def make_launch(
    *,
    caps: HarnessCapabilities,
    working_directory: str,
    resolved_executable: str | None = None,
) -> LaunchSnapshot:
    return LaunchSnapshot(
        harness_version=caps.version,
        working_directory=working_directory,
        adapter_version=__version__,
        capabilities=caps,
        resolved_executable=resolved_executable,
    )


def unique_prompt(prefix: str) -> str:
    return (
        f"{prefix} token={uuid4().hex[:12]}. Use the native shell tool to run `pwd` "
        "and request permission through the provider before completing."
    )


async def drain_until_terminal(
    events: AsyncIterator[Any],
    *,
    timeout: float = 180.0,
) -> list[Any]:
    """Consume events through the first authoritative terminal payload."""
    collected: list[Any] = []

    async def _run() -> list[Any]:
        async for item in events:
            collected.append(item)
            if getattr(item, "type", None) in TERMINAL_TYPES:
                assert getattr(item, "type", None) == "turn_completed", (
                    f"live turn ended with {getattr(item, 'type', None)}"
                )
                return collected
        return collected

    return await asyncio.wait_for(_run(), timeout=timeout)


def assert_no_duplicate_first_turn(
    first_events: list[Any],
    second_events: list[Any],
    *,
    first_turn_id: UUID,
) -> None:
    """Ensure the resumed turn stream does not replay the first turn's terminal."""
    first_terminals = [
        item
        for item in first_events
        if getattr(item, "type", None) in TERMINAL_TYPES
        and getattr(item, "turn_id", None) == first_turn_id
    ]
    assert first_terminals, "first turn did not produce a terminal event"
    replayed = [
        item
        for item in second_events
        if getattr(item, "type", None) in TERMINAL_TYPES
        and getattr(item, "turn_id", None) == first_turn_id
    ]
    assert not replayed, "first turn terminal was replayed after resume"


async def _answer_interaction(adapter: Any, session: HarnessSession, item: object) -> bool:
    """Answer one deferred interaction as soon as it appears on the stream."""
    if isinstance(item, HarnessInteractionRequest):
        interaction_id = item.payload.interaction_id
    elif isinstance(item, InteractionRequestedPayload):
        interaction_id = item.interaction_id
    else:
        return False
    await adapter.answer_interaction(
        session,
        InteractionAnswer(
            interaction_id=interaction_id,
            decision=ApprovalDecision.ALLOW_ONCE,
        ),
    )
    return True


async def collect_turn(
    adapter: Any,
    session: HarnessSession,
    *,
    timeout: float = 180.0,
) -> list[Any]:
    """Drain one turn, answering interactions until a terminal event arrives."""
    events: list[Any] = []
    interaction_seen = False

    async def _run() -> None:
        nonlocal interaction_seen
        async for item in adapter.events(session):
            events.append(item)
            if await _answer_interaction(adapter, session, item):
                interaction_seen = True
            event_type = getattr(item, "type", None)
            if event_type in TERMINAL_TYPES:
                assert event_type == "turn_completed", f"live turn ended with {event_type}"
                return
        raise AssertionError("live event stream ended without a terminal event")

    await asyncio.wait_for(_run(), timeout=timeout)
    assert interaction_seen, "live turn completed without a deferred interaction"
    return events

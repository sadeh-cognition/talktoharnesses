"""Shared helpers for opt-in live create/resume/interaction gates."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any, cast
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
    token = uuid4().hex[:12]
    # Workspace-relative write keeps Claude from treating /tmp markers as injection.
    # Explicit shell + python3 keeps broker approvals on Codex/Grok/Cursor allowlists.
    return (
        f"{prefix} token={token}. Use the native shell tool to run "
        f"`python3 -c \"open('live-{token}.txt','w').write('live-ok')\"` "
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
    payload: InteractionRequestedPayload | None = None
    if isinstance(item, HarnessInteractionRequest):
        payload = item.payload
    elif isinstance(item, InteractionRequestedPayload):
        payload = item
    else:
        return False
    request = payload.request
    if getattr(request, "kind", None) == "structured_question":
        answers: list[list[str]] = []
        for question in getattr(request, "questions", ()) or ():
            options: list[object] = []
            if isinstance(question, dict):
                q = cast(dict[str, Any], question)
                maybe_options = q.get("options")
                if isinstance(maybe_options, list):
                    options.extend(cast(list[object], maybe_options))
            label = "yes"
            if options:
                first = options[0]
                if isinstance(first, dict):
                    option_label = cast(dict[str, Any], first).get("label")
                    if isinstance(option_label, str):
                        label = option_label
                elif isinstance(first, str):
                    label = first
            answers.append([label])
        await adapter.answer_interaction(
            session,
            InteractionAnswer(
                interaction_id=payload.interaction_id,
                answers={"answers": answers or [["yes"]]},
            ),
        )
    else:
        await adapter.answer_interaction(
            session,
            InteractionAnswer(
                interaction_id=payload.interaction_id,
                decision=ApprovalDecision.ALLOW_ONCE,
            ),
        )
    return True


async def collect_turn(
    adapter: Any,
    session: HarnessSession,
    *,
    timeout: float = 180.0,
    require_interaction: bool = True,
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
    if require_interaction:
        assert interaction_seen, "live turn completed without a deferred interaction"
    return events

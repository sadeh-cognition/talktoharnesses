"""Shared helpers for opt-in live create/resume/feature gates."""

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
from talktoharnesses.providers.adapter import (
    HarnessInteractionRequest,
    HarnessSession,
    SteerRequest,
    TurnRequest,
)

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


def unique_multi_prompt(prefix: str) -> str:
    token = uuid4().hex[:12]
    return (
        f"{prefix} token={token}. Use the native shell tool twice: first run "
        f"`python3 -c \"open('live-{token}-a.txt','w').write('a')\"` then "
        f"`python3 -c \"open('live-{token}-b.txt','w').write('b')\"`. "
        "Request permission through the provider for each command before completing."
    )


def unique_busy_prompt(prefix: str, *, use_shell: bool) -> str:
    if use_shell:
        return (
            f"{prefix}. Use the native shell tool to run "
            '`python3 -c "import time; time.sleep(30)"` '
            "and request permission before running it. Do not finish until it completes."
        )
    return (
        f"{prefix}. Count slowly from 1 to 400 in your reply, one number per line, "
        "without using tools."
    )


def unique_nested_prompt(prefix: str) -> str:
    token = uuid4().hex[:12]
    return (
        f"{prefix} token={token}. Use a subagent or task tool to write "
        f"`live-{token}.txt` containing live-ok, then finish."
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
    min_interactions: int = 0,
    expected_terminal: str = "turn_completed",
) -> list[Any]:
    """Drain one turn, answering interactions until a terminal event arrives."""
    events: list[Any] = []
    interaction_count = 0

    async def _run() -> None:
        nonlocal interaction_count
        async for item in adapter.events(session):
            events.append(item)
            if await _answer_interaction(adapter, session, item):
                interaction_count += 1
            event_type = getattr(item, "type", None)
            if event_type in TERMINAL_TYPES:
                assert event_type == expected_terminal, f"live turn ended with {event_type}"
                return
        raise AssertionError("live event stream ended without a terminal event")

    await asyncio.wait_for(_run(), timeout=timeout)
    required = max(1 if require_interaction else 0, min_interactions)
    if required:
        assert interaction_count >= required, (
            f"live turn completed with {interaction_count} interactions; expected >= {required}"
        )
    return events


async def _drain_busy_turn(
    adapter: Any,
    session: HarnessSession,
    *,
    on_progress: Any,
    expected_terminal: str,
    timeout: float,
) -> list[Any]:
    events: list[Any] = []
    progressed = False

    async def _run() -> None:
        nonlocal progressed
        async for item in adapter.events(session):
            events.append(item)
            await _answer_interaction(adapter, session, item)
            if not progressed:
                progressed = True
                await on_progress()
            event_type = getattr(item, "type", None)
            if event_type in TERMINAL_TYPES:
                assert event_type == expected_terminal, f"live turn ended with {event_type}"
                return
        raise AssertionError("live event stream ended without a terminal event")

    await asyncio.wait_for(_run(), timeout=timeout)
    return events


async def exercise_advertised_features(
    adapter: Any,
    session: HarnessSession,
    caps: HarnessCapabilities,
    *,
    use_shell: bool = True,
) -> None:
    """Prove each advertised capability that has a published live gate."""
    if caps.supports_multi_interaction:
        await adapter.submit(
            session,
            TurnRequest(turn_id=uuid4(), prompt=unique_multi_prompt("multi-turn")),
        )
        await collect_turn(adapter, session, min_interactions=2)

    if caps.supports_nested_activity:
        await adapter.submit(
            session,
            TurnRequest(turn_id=uuid4(), prompt=unique_nested_prompt("nested-turn")),
        )
        nested_events = await collect_turn(adapter, session, require_interaction=False)
        assert any(getattr(item, "type", None) == "activity_started" for item in nested_events), (
            "nested-activity gate did not observe activity_started"
        )

    if caps.supports_steer:
        steer_turn = uuid4()
        await adapter.submit(
            session,
            TurnRequest(
                turn_id=steer_turn,
                prompt=unique_busy_prompt("steer-turn", use_shell=use_shell),
            ),
        )
        steered = False

        async def _steer() -> None:
            nonlocal steered
            ok = await adapter.steer(
                session,
                SteerRequest(
                    turn_id=steer_turn,
                    prompt="Stop waiting and reply with the single word done.",
                ),
            )
            assert ok is True, "advertised steer returned False"
            steered = True

        await _drain_busy_turn(
            adapter,
            session,
            on_progress=_steer,
            expected_terminal="turn_completed",
            timeout=180.0,
        )
        assert steered

    if caps.supports_interrupt:
        await adapter.submit(
            session,
            TurnRequest(
                turn_id=uuid4(),
                prompt=unique_busy_prompt("interrupt-turn", use_shell=use_shell),
            ),
        )

        async def _interrupt() -> None:
            await adapter.interrupt(session)

        await _drain_busy_turn(
            adapter,
            session,
            on_progress=_interrupt,
            expected_terminal="turn_interrupted",
            timeout=60.0,
        )

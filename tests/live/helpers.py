"""Shared helpers for opt-in live create/resume/feature gates over HTTP."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid4

import pytest

from talktoharnesses.client import AsyncTalkToHarnessesClient, ConversationStreamItem
from talktoharnesses.domain.enums import ApprovalDecision
from talktoharnesses.domain.events import (
    ConversationEvent,
    InteractionRequestedPayload,
    SessionResumedPayload,
    SessionStartedPayload,
    event_turn_id,
)
from talktoharnesses.domain.models import (
    HarnessCapabilities,
    HarnessConfiguration,
    HarnessProjection,
)

TERMINAL_TYPES = frozenset(
    {"turn_completed", "turn_failed", "turn_interrupted", "turn_outcome_unknown"}
)


@dataclass(frozen=True)
class LiveHttp:
    client: AsyncTalkToHarnessesClient
    workspace: Path
    close_runtime: Callable[[UUID], Awaitable[None]]


class CompatibilityRelease(Protocol):
    id: str

    def to_harness_capabilities(self) -> HarnessCapabilities: ...


class LiveStream:
    """Pull one SSE iterator sequentially and resolve interactions as they arrive."""

    def __init__(
        self,
        items: AsyncIterator[ConversationStreamItem],
        on_event: Callable[[ConversationEvent], Awaitable[None]],
    ) -> None:
        self._items = items
        self._on_event = on_event

    async def wait_until(
        self,
        predicate: Callable[[ConversationEvent], bool],
        *,
        timeout: float = 180.0,
    ) -> list[ConversationEvent]:
        collected: list[ConversationEvent] = []

        async def _run() -> None:
            async for item in self._items:
                if not isinstance(item, ConversationEvent):
                    continue
                collected.append(item)
                await self._on_event(item)
                if predicate(item):
                    return
            raise AssertionError("live event stream ended before expected event")

        await asyncio.wait_for(_run(), timeout=timeout)
        return collected

    async def collect_turn(
        self,
        turn_id: UUID,
        *,
        expected_terminal: str = "turn_completed",
        timeout: float = 180.0,
        min_interactions: int = 0,
    ) -> list[ConversationEvent]:
        window = await self.wait_until(
            lambda event: event.type in TERMINAL_TYPES and event_turn_id(event) == turn_id,
            timeout=timeout,
        )
        return _assert_turn(
            window,
            turn_id,
            expected_terminal=expected_terminal,
            min_interactions=min_interactions,
        )

    async def collect_busy_turn(
        self,
        turn_id: UUID,
        *,
        on_progress: Callable[[], Awaitable[None]],
        expected_terminal: str,
        timeout: float = 180.0,
    ) -> list[ConversationEvent]:
        collected: list[ConversationEvent] = []
        progressed = False

        async def _run() -> None:
            nonlocal progressed
            async for item in self._items:
                if not isinstance(item, ConversationEvent):
                    continue
                collected.append(item)
                await self._on_event(item)
                if (
                    not progressed
                    and item.type == "turn_started"
                    and event_turn_id(item) == turn_id
                ):
                    progressed = True
                    await on_progress()
                if item.type in TERMINAL_TYPES and event_turn_id(item) == turn_id:
                    assert item.type == expected_terminal, f"live turn ended with {item.type}"
                    return
            raise AssertionError("live event stream ended before expected event")

        await asyncio.wait_for(_run(), timeout=timeout)
        assert progressed, "busy turn made no progress before terminal"
        return collected


AfterCreateHook = Callable[
    [LiveStream, AsyncTalkToHarnessesClient, UUID],
    Awaitable[None],
]


def unique_prompt(prefix: str, *, mention_permission: bool = True) -> str:
    token = uuid4().hex[:12]
    # Workspace-relative write keeps Claude from treating /tmp markers as injection.
    # Explicit shell + python3 keeps broker approvals on Codex/Grok/Cursor allowlists.
    if mention_permission:
        return (
            f"{prefix} token={token}. Use the native shell tool to run "
            f"`python3 -c \"open('live-{token}.txt','w').write('live-ok')\"` "
            "and request permission through the provider before completing."
        )
    return (
        f"{prefix} token={token}. Your only task is to invoke the native shell tool "
        "with this exact command: "
        f"`python3 -c \"open('live-{token}.txt','w').write('live-ok')\"`. "
        "Do not respond with text before invoking the tool."
    )


def unique_multi_prompt(prefix: str, *, mention_permission: bool = True) -> str:
    token = uuid4().hex[:12]
    if mention_permission:
        return (
            f"{prefix} token={token}. Use the native shell tool twice: first run "
            f"`python3 -c \"open('live-{token}-a.txt','w').write('a')\"` then "
            f"`python3 -c \"open('live-{token}-b.txt','w').write('b')\"`. "
            "Request permission through the provider for each command before completing."
        )
    return (
        f"{prefix} token={token}. Use the native shell tool twice: first run "
        f"`python3 -c \"open('live-{token}-a.txt','w').write('a')\"` then "
        f"`python3 -c \"open('live-{token}-b.txt','w').write('b')\"`. "
        "Do not respond with text before making both tool calls."
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


def unique_text_prompt(prefix: str) -> str:
    token = f"{prefix}-{uuid4().hex[:12]}"
    return f"Reply with exactly this token and do not use tools: {token}"


def require_executable(env_name: str) -> str:
    path = os.environ.get(env_name)
    if not path:
        pytest.fail(f"{env_name} is required when live tests are enabled")
    return path


def release_id_for_version(releases: Sequence[CompatibilityRelease], version: str) -> str:
    matched = [item.id for item in releases if item.to_harness_capabilities().version == version]
    assert matched, "probed version is not a packaged compatibility release"
    return matched[0]


def _idempotency_key(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex}"


def _assert_turn(
    window: Sequence[ConversationEvent],
    turn_id: UUID,
    *,
    expected_terminal: str,
    min_interactions: int,
) -> list[ConversationEvent]:
    matching = [event for event in window if event_turn_id(event) == turn_id]
    terminals = [event for event in matching if event.type in TERMINAL_TYPES]
    assert terminals, "live turn did not produce a terminal event"
    assert terminals[0].type == expected_terminal, f"live turn ended with {terminals[0].type}"
    interactions = [event for event in matching if event.type == "interaction_requested"]
    if min_interactions:
        assert len(interactions) >= min_interactions, (
            f"live turn completed with {len(interactions)} interactions; "
            f"expected >= {min_interactions}"
        )
    return matching


async def _resolve_interaction(
    client: AsyncTalkToHarnessesClient,
    conversation_id: UUID,
    event: ConversationEvent,
) -> None:
    payload = event.payload
    if not isinstance(payload, InteractionRequestedPayload):
        return
    request = payload.request
    if request.kind == "structured_question":
        answers: dict[str, list[str]] = {}
        for question in request.questions:
            options = question.options
            value = options[0].value if options else "yes"
            answers[question.id] = [value]
        await client.resolve_interaction(
            conversation_id,
            payload.interaction_id,
            answers=answers,
        )
        return
    await client.resolve_interaction(
        conversation_id,
        payload.interaction_id,
        decision=ApprovalDecision.ALLOW_ONCE,
    )


def _session_native_id(
    events: Sequence[ConversationEvent],
    payload_type: type[SessionStartedPayload] | type[SessionResumedPayload],
) -> str:
    for event in events:
        if isinstance(event.payload, payload_type) and event.payload.native_session_id:
            return event.payload.native_session_id
    raise AssertionError(f"missing {payload_type.__name__} native_session_id")


async def exercise_advertised_features(
    stream: LiveStream,
    client: AsyncTalkToHarnessesClient,
    conversation_id: UUID,
    caps: HarnessCapabilities,
    *,
    use_shell: bool = True,
    mention_permission: bool = True,
) -> None:
    """Prove each advertised capability that has a published live gate."""
    if caps.supports_multi_interaction:
        submitted = await client.submit_turn(
            conversation_id,
            prompt=unique_multi_prompt(
                "multi-turn",
                mention_permission=mention_permission,
            ),
            idempotency_key=_idempotency_key("multi"),
        )
        await stream.collect_turn(submitted.turn.id, min_interactions=2)

    if caps.supports_nested_activity:
        submitted = await client.submit_turn(
            conversation_id,
            prompt=unique_nested_prompt("nested-turn"),
            idempotency_key=_idempotency_key("nested"),
        )
        nested_events = await stream.collect_turn(submitted.turn.id)
        assert any(event.type == "activity_started" for event in nested_events), (
            "nested-activity gate did not observe activity_started"
        )

    if caps.supports_steer:
        submitted = await client.submit_turn(
            conversation_id,
            prompt=unique_busy_prompt("steer-turn", use_shell=use_shell),
            idempotency_key=_idempotency_key("steer-submit"),
        )

        async def _steer() -> None:
            await client.steer(
                conversation_id,
                prompt="Stop waiting and reply with the single word done.",
                idempotency_key=_idempotency_key("steer"),
            )

        await stream.collect_busy_turn(
            submitted.turn.id,
            on_progress=_steer,
            expected_terminal="turn_completed",
        )

    if caps.supports_interrupt:
        submitted = await client.submit_turn(
            conversation_id,
            prompt=unique_busy_prompt("interrupt-turn", use_shell=use_shell),
            idempotency_key=_idempotency_key("interrupt-submit"),
        )

        async def _interrupt() -> None:
            await client.interrupt(conversation_id)

        await stream.collect_busy_turn(
            submitted.turn.id,
            on_progress=_interrupt,
            expected_terminal="turn_interrupted",
            timeout=60.0,
        )


async def run_live_gate(
    live: LiveHttp,
    *,
    configuration: HarnessConfiguration,
    releases: Sequence[CompatibilityRelease],
    expected_release_id: str | None = None,
    min_create_interactions: int = 1,
    min_resume_interactions: int = 1,
    use_shell: bool = True,
    mention_permission: bool = True,
    prompt_fn: Callable[[str], str] = unique_prompt,
    after_create: AfterCreateHook | None = None,
) -> HarnessProjection:
    """Create, probe, turn, close runtime, resume, and exercise advertised features."""
    client = live.client
    harness = await client.create_harness(
        name=f"live-{configuration.kind.value}",
        configuration=configuration,
    )
    probe = await client.probe_harness(harness.id, timeout=120.0)
    caps = probe.capabilities
    assert caps.supports_resume is True
    release_id = release_id_for_version(releases, caps.version)
    if expected_release_id is not None:
        assert release_id == expected_release_id
    print(f"detected_release_id={release_id}")

    snapshot = await client.create_conversation(harness.id, title="live-gate")
    conversation_id = snapshot.detail.conversation.id
    items = client.stream_conversation_events(conversation_id)

    async def on_event(event: ConversationEvent) -> None:
        await _resolve_interaction(client, conversation_id, event)

    stream = LiveStream(items, on_event)
    try:
        created = await client.submit_turn(
            conversation_id,
            prompt=prompt_fn("create-turn"),
            idempotency_key=_idempotency_key("create"),
        )
        first_window = await stream.wait_until(
            lambda event: event.type in TERMINAL_TYPES and event_turn_id(event) == created.turn.id,
        )
        _assert_turn(
            first_window,
            created.turn.id,
            expected_terminal="turn_completed",
            min_interactions=min_create_interactions,
        )
        first_native = _session_native_id(first_window, SessionStartedPayload)
        if after_create is not None:
            await after_create(stream, client, conversation_id)

        await live.close_runtime(conversation_id)
        resumed_turn = await client.submit_turn(
            conversation_id,
            prompt=prompt_fn("resume-turn"),
            idempotency_key=_idempotency_key("resume"),
        )
        resume_window = await stream.wait_until(
            lambda event: (
                event.type in TERMINAL_TYPES and event_turn_id(event) == resumed_turn.turn.id
            ),
        )
        assert any(event.type == "session_closed" for event in resume_window), (
            "runtime close did not produce session_closed before resume"
        )
        _assert_turn(
            resume_window,
            resumed_turn.turn.id,
            expected_terminal="turn_completed",
            min_interactions=min_resume_interactions,
        )
        resumed_native = _session_native_id(resume_window, SessionResumedPayload)
        assert resumed_native == first_native
        assert not any(event.type == "session_started" for event in resume_window), (
            "resume spawned a new session instead of session_resumed"
        )
        replayed = [
            event
            for event in resume_window
            if event.type in TERMINAL_TYPES and event_turn_id(event) == created.turn.id
        ]
        assert not replayed, "first turn terminal was replayed after resume"
        await exercise_advertised_features(
            stream,
            client,
            conversation_id,
            caps,
            use_shell=use_shell,
            mention_permission=mention_permission,
        )
    finally:
        closer = getattr(items, "aclose", None)
        if closer is not None:
            await closer()

    print(f"live_gate_passed release_id={release_id}")
    return harness

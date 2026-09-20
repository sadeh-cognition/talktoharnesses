"""Shared helpers for opt-in live create/resume/feature gates over HTTP."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Generator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from tth_types.sandbox import SandboxPolicy, SaveSandboxPolicy

from talktoharnesses.client import AsyncTalkToHarnessesClient, ConversationStreamItem
from talktoharnesses.domain.enums import ApprovalDecision, HarnessKind
from talktoharnesses.domain.events import (
    ConversationEvent,
    InteractionRequestedPayload,
    SessionResumedPayload,
    SessionStartedPayload,
    UsageUpdatedPayload,
    event_turn_id,
)
from talktoharnesses.domain.models import (
    HarnessCapabilities,
    HarnessConfiguration,
    HarnessProjection,
)
from talktoharnesses.remote.isolated_sandbox import IsolatedSandbox
from talktoharnesses.remote.scoped_sandboxes import ScopedSandboxManager

TERMINAL_TYPES = frozenset(
    {"turn_completed", "turn_failed", "turn_interrupted", "turn_outcome_unknown"}
)


@dataclass(frozen=True)
class LiveHttp:
    client: AsyncTalkToHarnessesClient
    workspace: Path
    close_runtime: Callable[[UUID], Awaitable[None]]


@contextmanager
def isolated_sandbox_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
    *,
    kind: HarnessKind,
    auth_environment_variable: str,
    default_auth_path: Path,
    credential_environment_variable: str | None = None,
) -> Generator[None]:
    auth_file = os.environ.get(auth_environment_variable)
    if auth_file is None:
        home = os.environ.get("HOME")
        if home is None:
            pytest.fail(
                f"{kind.value} sandbox live test requires HOME or {auth_environment_variable}"
            )
        auth_file = str(Path(home) / default_auth_path)
    if not Path(auth_file).is_file():
        pytest.fail(f"{kind.value} sandbox auth file was not found: {auth_file}")

    scopes: set[str] = set()
    resolve = ScopedSandboxManager.for_configuration

    async def track_scope(
        manager: ScopedSandboxManager, configuration: HarnessConfiguration
    ) -> IsolatedSandbox:
        sandbox = await resolve(manager, configuration)
        scopes.add(sandbox.name)
        monkeypatch.setenv(LIVE_CONTAINER_ENV, sandbox.name)
        return sandbox

    if credential_environment_variable is not None:
        monkeypatch.delenv(credential_environment_variable, raising=False)
    monkeypatch.setenv(auth_environment_variable, auth_file)
    monkeypatch.setenv("TTH_SANDBOX_MOUNT_ROOTS", str(tmp_path_factory.getbasetemp()))
    monkeypatch.setenv("TTH_SANDBOX_STATE_DIR", str(tmp_path_factory.mktemp("gateway-state")))
    monkeypatch.setattr(ScopedSandboxManager, "for_configuration", track_scope)
    try:
        yield
    finally:
        import docker
        from docker.errors import NotFound

        client = docker.from_env()
        try:
            for container_name in scopes:
                for suffix in ("-gateway", ""):
                    with suppress(NotFound):
                        client.containers.get(container_name + suffix).remove(force=True)
                with suppress(NotFound):
                    client.networks.get(container_name + "-network").remove()
                for suffix in ("home", "data"):
                    with suppress(NotFound):
                        client.volumes.get(f"{container_name}-{suffix}").remove()
        finally:
            client.close()


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
        require_usage: bool = False,
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
            require_usage=require_usage,
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


# The isolated sandbox container's name, for tests that inspect it directly.
LIVE_CONTAINER_ENV = "TALKTOHARNESSES_LIVE_SANDBOX_CONTAINER"

RTK_REWRITE_PROMPT = (
    "Your only task is to invoke the native shell tool with this exact command: "
    "`git status`. Do not add flags, do not run anything else, and do not respond "
    "with text before invoking the tool."
)


async def assert_rtk_rewrite(
    stream: LiveStream,
    client: AsyncTalkToHarnessesClient,
    conversation_id: UUID,
) -> None:
    """Prove RTK rewrote a shell command inside the sandbox (``git status`` → ``rtk git status``).

    Usable as ``after_create`` for kinds with a transparent RTK hook/plugin.
    Event streams report the model's original tool call, so the evidence is
    RTK's own history in the sandbox home: every rewrite it executes is logged
    there and ``rtk gain --history`` lists it.
    """
    submitted = await client.submit_turn(
        conversation_id,
        prompt=RTK_REWRITE_PROMPT,
        idempotency_key=f"rtk-rewrite-{conversation_id}",
    )
    events = await stream.collect_turn(submitted.turn.id)
    tool_events = [
        (event.type, event.model_dump_json()[:400])
        for event in events
        if event.type.startswith(("tool_", "interaction_"))
    ]
    assert tool_events, (
        f"the harness ran no tool for the rtk prompt; assistant text: {assistant_text(events)!r}"
    )
    history = await asyncio.to_thread(_sandbox_rtk_history)
    assert "rtk git status" in history, (
        "rtk did not rewrite `git status` inside the sandbox; "
        f"rtk history: {history!r}; tool events: {tool_events}"
    )


def _sandbox_rtk_history() -> str:
    """``rtk gain --history`` output from the live sandbox container's home."""
    import docker

    container_name = os.environ[LIVE_CONTAINER_ENV]
    client = docker.from_env()
    try:
        container = client.containers.get(container_name)
        exit_code, output = container.exec_run(["rtk", "gain", "--history"], user="agent")
    finally:
        client.close()
    raw = output if isinstance(output, bytes) else b"".join(output)
    text = raw.decode("utf-8", errors="replace")
    assert exit_code == 0, f"rtk gain --history failed in {container_name}: {text}"
    return text


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


def unique_edit_prompt(prefix: str) -> str:
    """Drive the harness's native file-write tool rather than the shell.

    Grok forwards its Write/SearchReplace tool inputs on permission requests
    (captured at 1.0.13), so this prompt proves those shapes stay allowlisted.
    """
    token = uuid4().hex[:12]
    return (
        f"{prefix} token={token}. Use your native file write tool (not the shell) to "
        f"create `live-{token}.md` in the current directory containing exactly "
        "`live-ok`. Do not ask any questions."
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
        f"{prefix}. Count slowly from 1 to 800 in your reply, one number per line, "
        "without using tools. Do not skip or summarize; write every number."
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


def _idempotency_key(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex}"


def _assert_turn(
    window: Sequence[ConversationEvent],
    turn_id: UUID,
    *,
    expected_terminal: str,
    min_interactions: int,
    require_usage: bool = False,
) -> list[ConversationEvent]:
    matching = [event for event in window if event_turn_id(event) == turn_id]
    terminals = [event for event in matching if event.type in TERMINAL_TYPES]
    assert terminals, "live turn did not produce a terminal event"
    assert terminals[0].type == expected_terminal, (
        f"live turn ended with {terminals[0].type}: {terminals[0].model_dump_json()[:1500]}"
    )
    interactions = [event for event in matching if event.type == "interaction_requested"]
    if min_interactions:
        assert len(interactions) >= min_interactions, (
            f"live turn completed with {len(interactions)} interactions; "
            f"expected >= {min_interactions}; events: {[event.type for event in matching]}; "
            f"assistant text: {assistant_text(matching)!r}"
        )
    if require_usage:
        _assert_token_usage(matching, terminals[0])
    return matching


def assistant_text(events: Sequence[ConversationEvent]) -> str:
    parts: list[str] = []
    for event in events:
        payload = event.payload
        text = getattr(payload, "text", None) or getattr(payload, "delta", None)
        if isinstance(text, str) and event.type.startswith("assistant_message"):
            parts.append(text)
    return "".join(parts)[:2000]


def _assert_token_usage(
    matching: Sequence[ConversationEvent],
    terminal: ConversationEvent,
) -> None:
    usage_events: list[tuple[int, UsageUpdatedPayload]] = []
    for index, event in enumerate(matching):
        if isinstance(event.payload, UsageUpdatedPayload):
            usage_events.append((index, event.payload))
    assert usage_events, (
        "live turn did not produce usage_updated before terminal; "
        f"events: {[event.type for event in matching]}"
    )
    usage_index, usage = usage_events[-1]
    assert usage_index < matching.index(terminal), "live turn produced usage_updated after terminal"
    values: tuple[int | None, ...] = (
        usage.input_tokens,
        usage.output_tokens,
        usage.total_tokens,
        usage.cached_input_tokens,
    )
    reported = [value for value in values if value is not None]
    assert reported, "live usage_updated did not report any token values"
    assert all(value >= 0 for value in reported), "live usage_updated reported negative tokens"
    assert any(value > 0 for value in reported), "live usage_updated reported only zero tokens"


async def resolve_interaction(
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
    min_create_interactions: int = 1,
    min_resume_interactions: int = 1,
    use_shell: bool = True,
    mention_permission: bool = True,
    prompt_fn: Callable[[str], str] = unique_prompt,
    after_create: AfterCreateHook | None = None,
) -> HarnessProjection:
    """Create, probe, turn, close runtime, resume, and exercise advertised features."""
    client = live.client
    configuration = await scoped_configuration(client, configuration)
    harness = await client.create_harness(
        name=f"live-{configuration.kind.value}",
        configuration=configuration,
    )
    probe = await client.probe_harness(harness.id, timeout=120.0)
    caps = probe.capabilities
    assert caps.supports_resume is True
    print(f"probed_version={caps.version}")
    if probe.version_advisory is not None:
        print(f"version_advisory={probe.version_advisory.status}")

    snapshot = await client.create_conversation(harness.id, title="live-gate")
    conversation_id = snapshot.detail.conversation.id
    items = client.stream_conversation_events(conversation_id)

    async def on_event(event: ConversationEvent) -> None:
        await resolve_interaction(client, conversation_id, event)

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
            require_usage=True,
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
            require_usage=True,
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

    print(f"live_gate_passed probed_version={caps.version}")
    return harness


async def scoped_configuration(
    client: AsyncTalkToHarnessesClient, configuration: HarnessConfiguration
) -> HarnessConfiguration:
    revision = await client.save_sandbox_policy(
        uuid4(),
        SaveSandboxPolicy(
            policy=SandboxPolicy(project_root=configuration.working_directory),
            expected_revision=0,
        ),
    )
    return configuration.model_copy(update={"sandbox_policy": revision.ref})

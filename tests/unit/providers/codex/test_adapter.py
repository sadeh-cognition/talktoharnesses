"""Codex adapter unit tests with a fake public SDK surface."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from types import SimpleNamespace
from uuid import uuid4

import pytest

from talktoharnesses.domain.enums import ApprovalDecision, ErrorCode, HarnessKind
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.domain.events import HarnessEvent, TurnCompletedPayload, TurnFailedPayload
from talktoharnesses.domain.models import (
    HarnessCapabilities,
    HarnessConfiguration,
    InteractionAnswer,
    LaunchSnapshot,
)
from talktoharnesses.providers.adapter import (
    HarnessInteractionRequest,
    HarnessSession,
    StartSessionRequest,
    SteerRequest,
    TurnRequest,
)
from talktoharnesses.providers.codex.adapter import CodexAdapter
from talktoharnesses.providers.codex.normalizer import CodexNormalizer


@dataclass
class FakeTurnHandle:
    id: str
    thread_id: str
    prompt: str
    events: list[dict[str, object]] = field(default_factory=list[dict[str, object]])
    steered: list[str] = field(default_factory=list[str])
    interrupted: bool = False
    options: dict[str, object] = field(default_factory=dict[str, object])

    def stream(self) -> AsyncIterator[dict[str, object]]:
        async def _gen() -> AsyncIterator[dict[str, object]]:
            for event in self.events:
                yield event
            yield {
                "method": "turnCompleted",
                "thread_id": self.thread_id,
                "turn_id": self.id,
                "status": "completed",
                "final_response": None,
            }

        return _gen()

    async def steer(self, prompt: str) -> None:
        self.steered.append(prompt)

    async def interrupt(self) -> None:
        self.interrupted = True


@dataclass
class FakeThread:
    id: str
    handles: list[FakeTurnHandle] = field(default_factory=list[FakeTurnHandle])

    async def turn(self, prompt: str, **options: object) -> FakeTurnHandle:
        handle = FakeTurnHandle(
            id=f"turn-{len(self.handles) + 1}",
            thread_id=self.id,
            prompt=prompt,
            options=options,
        )
        self.handles.append(handle)
        return handle


class FakeCodex:
    instances: list[FakeCodex] = []

    def __init__(self) -> None:
        self.closed = False
        self.threads: list[FakeThread] = []
        self.start_kwargs: dict[str, object] = {}
        self._id = str(uuid4())
        FakeCodex.instances.append(self)

    async def __aenter__(self) -> FakeCodex:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        self.closed = True

    async def close(self) -> None:
        self.closed = True

    async def thread_start(self, **kwargs: object) -> FakeThread:
        self.start_kwargs = kwargs
        thread = FakeThread(id=f"thread-{self._id}")
        self.threads.append(thread)
        return thread

    async def thread_resume(self, thread_id: str, **kwargs: object) -> FakeThread:
        del kwargs
        thread = FakeThread(id=thread_id)
        self.threads.append(thread)
        return thread


def _config() -> HarnessConfiguration:
    return HarnessConfiguration(
        kind=HarnessKind.CODEX,
        working_directory="/tmp",
        effort="high",
    )


def _launch() -> LaunchSnapshot:
    return LaunchSnapshot(
        harness_version="0.144.4",
        working_directory="/tmp",
        adapter_version="2026.8.1",
        capabilities=HarnessCapabilities(kind=HarnessKind.CODEX, version="0.144.4"),
    )


@pytest.mark.asyncio
async def test_start_submit_terminal_without_final_and_steer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeCodex.instances.clear()

    # Bypass real probe version matching.
    async def fake_probe(config: HarnessConfiguration):
        from talktoharnesses.providers.codex.compatibility import match_release

        release = match_release(sdk_version="0.144.4", runtime_version="0.144.4", platform="linux")
        return release.to_harness_capabilities(), release

    monkeypatch.setattr("talktoharnesses.providers.codex.adapter.probe_codex", fake_probe)

    adapter = CodexAdapter(client_factory=FakeCodex)
    caps = await adapter.probe(_config())
    assert caps.supports_steer is True
    session = await adapter.start(
        StartSessionRequest(
            conversation_id=uuid4(),
            binding_id=uuid4(),
            configuration=_config(),
            launch=_launch(),
        )
    )
    assert session.native_session_id is not None
    assert session.native_session_id.startswith("thread-")
    from openai_codex import ApprovalMode, Sandbox

    assert FakeCodex.instances[0].start_kwargs["approval_mode"] is ApprovalMode.auto_review
    assert FakeCodex.instances[0].start_kwargs["sandbox"] is Sandbox.workspace_write
    turn_id = uuid4()
    await adapter.submit(session, TurnRequest(turn_id=turn_id, prompt="hi"))
    assert session.effort == "high"
    effort = FakeCodex.instances[0].threads[0].handles[0].options["effort"]
    assert effort.value == "high"  # type: ignore[attr-defined]
    assert await adapter.steer(session, SteerRequest(turn_id=turn_id, prompt="more"))
    # Drain until terminal.
    events: list[HarnessEvent | HarnessInteractionRequest] = []
    async for item in adapter.events(session):
        events.append(item)
        if isinstance(item, TurnCompletedPayload):
            break
    completed = next(e for e in events if isinstance(e, TurnCompletedPayload))
    assert completed.has_assistant_message is False
    await adapter.close(session)
    assert FakeCodex.instances[0].closed is True


@pytest.mark.asyncio
async def test_two_conversations_isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeCodex.instances.clear()

    async def fake_probe(config: HarnessConfiguration):
        from talktoharnesses.providers.codex.compatibility import match_release

        release = match_release(sdk_version="0.144.4", runtime_version="0.144.4", platform="linux")
        return release.to_harness_capabilities(), release

    monkeypatch.setattr("talktoharnesses.providers.codex.adapter.probe_codex", fake_probe)
    a1 = CodexAdapter(client_factory=FakeCodex)
    a2 = CodexAdapter(client_factory=FakeCodex)
    await a1.probe(_config())
    await a2.probe(_config())
    s1 = await a1.start(
        StartSessionRequest(
            conversation_id=uuid4(),
            binding_id=uuid4(),
            configuration=_config(),
            launch=_launch(),
        )
    )
    s2 = await a2.start(
        StartSessionRequest(
            conversation_id=uuid4(),
            binding_id=uuid4(),
            configuration=_config(),
            launch=_launch(),
        )
    )
    assert s1.native_session_id != s2.native_session_id
    assert FakeCodex.instances[0] is not FakeCodex.instances[1]
    await a1.close(s1)
    await a2.close(s2)


@pytest.mark.asyncio
async def test_stream_approval_notification_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_probe(config: HarnessConfiguration):
        from talktoharnesses.providers.codex.compatibility import match_release

        release = match_release(sdk_version="0.144.4", runtime_version="0.144.4", platform="linux")
        return release.to_harness_capabilities(), release

    monkeypatch.setattr("talktoharnesses.providers.codex.adapter.probe_codex", fake_probe)
    adapter = CodexAdapter(client_factory=FakeCodex)
    await adapter.probe(_config())
    session = await adapter.start(
        StartSessionRequest(
            conversation_id=uuid4(),
            binding_id=uuid4(),
            configuration=_config(),
            launch=_launch(),
        )
    )
    turn_id = uuid4()
    await adapter.submit(session, TurnRequest(turn_id=turn_id, prompt="hi"))
    assert adapter._thread is not None  # pyright: ignore[reportPrivateUsage]
    handle = adapter._thread.handles[0]  # pyright: ignore[reportPrivateUsage]
    handle.events.append(
        {
            "method": "approvalRequest",
            "request_id": "approval-1",
            "thread_id": session.native_session_id or "",
        }
    )

    async def _drain() -> list[HarnessEvent | HarnessInteractionRequest]:
        return [item async for item in adapter.events(session)]

    events = await asyncio.wait_for(_drain(), timeout=2.0)
    failed = next(event for event in events if isinstance(event, TurnFailedPayload))
    assert failed.turn_id == turn_id
    assert failed.error_code == ErrorCode.UNSUPPORTED_NATIVE_EVENT.value

    with pytest.raises(DomainError) as exc:
        await adapter.answer_interaction(
            session,
            InteractionAnswer(
                interaction_id=uuid4(),
                decision=ApprovalDecision.ALLOW_ONCE,
            ),
        )
    assert exc.value.code is ErrorCode.INVALID_STATE
    await adapter.close(session)


@pytest.mark.asyncio
async def test_brokered_approval_handler_awaits_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_probe(config: HarnessConfiguration):
        from talktoharnesses.providers.codex.compatibility import match_release

        release = match_release(sdk_version="0.144.4", runtime_version="0.144.4", platform="linux")
        return release.to_harness_capabilities(), release

    monkeypatch.setattr("talktoharnesses.providers.codex.adapter.probe_codex", fake_probe)
    adapter = CodexAdapter(client_factory=FakeCodex)
    await adapter.probe(_config())
    session = await adapter.start(
        StartSessionRequest(
            conversation_id=uuid4(),
            binding_id=uuid4(),
            configuration=_config(),
            launch=_launch(),
        )
    )
    # Keep an active turn without racing the fake stream terminal event.
    adapter._normalizer.begin_turn(uuid4())  # pyright: ignore[reportPrivateUsage]

    async def _answer_when_requested() -> None:
        async for item in adapter.events(session):
            if isinstance(item, HarnessInteractionRequest):
                await adapter.answer_interaction(
                    session,
                    InteractionAnswer(
                        interaction_id=item.payload.interaction_id,
                        decision=ApprovalDecision.ALLOW_ONCE,
                    ),
                )
                return

    answer_task = asyncio.create_task(_answer_when_requested())
    result = await asyncio.to_thread(
        adapter._approval_handler,  # pyright: ignore[reportPrivateUsage]
        "item/commandExecution/requestApproval",
        {
            "threadId": session.native_session_id,
            "turnId": "turn-1",
            "itemId": "cmd-1",
            "command": ["echo", "hi"],
            "cwd": "/tmp",
            "reason": "test",
        },
    )
    await asyncio.wait_for(answer_task, timeout=2.0)
    assert result == {"decision": "accept"}
    await adapter.close(session)


def test_normalizer_rejects_unknown_field() -> None:
    normalizer = CodexNormalizer()
    normalizer.set_session("t1")
    normalizer.begin_turn(uuid4())
    with pytest.raises(ValueError):
        normalizer.on_notification(
            {
                "method": "agentMessageDelta",
                "thread_id": "t1",
                "turn_id": "u1",
                "item_id": "i1",
                "delta": "hi",
                "extra": "nope",
            }
        )


def test_normalizer_skips_persisted_delta_with_matching_sequence_key() -> None:
    normalizer = CodexNormalizer()
    normalizer.set_session("t1")
    normalizer.import_seen(frozenset(), frozenset({"msg:i1:1"}))
    normalizer.begin_turn(uuid4())
    first = normalizer.on_notification(
        {
            "method": "agentMessageDelta",
            "thread_id": "t1",
            "turn_id": "u1",
            "item_id": "i1",
            "delta": "replayed",
        }
    )
    second = normalizer.on_notification(
        {
            "method": "agentMessageDelta",
            "thread_id": "t1",
            "turn_id": "u1",
            "item_id": "i1",
            "delta": "new",
        }
    )
    assert first == []
    assert any(getattr(event, "text", None) == "new" for event in second)


def test_coerce_public_slotted_notifications() -> None:
    from openai_codex.generated.v2_all import (
        AgentMessageDeltaNotification,
        AgentMessageThreadItem,
        ItemCompletedNotification,
        ItemStartedNotification,
        ReasoningTextDeltaNotification,
        ThreadItem,
        Turn,
        TurnCompletedNotification,
        TurnStartedNotification,
        TurnStatus,
    )
    from openai_codex.models import Notification

    adapter = CodexAdapter(client_factory=FakeCodex)
    turn = Turn(id="turn-1", items=[], items_view=None, status=TurnStatus.completed)
    item = ThreadItem(root=AgentMessageThreadItem(id="item-1", text="hello", type="agentMessage"))
    notifications = [
        Notification(
            method="turn/started",
            payload=TurnStartedNotification(thread_id="thread-1", turn=turn),
        ),
        Notification(
            method="item/started",
            payload=ItemStartedNotification(
                item=item,
                started_at_ms=1,
                thread_id="thread-1",
                turn_id="turn-1",
            ),
        ),
        Notification(
            method="item/agentMessage/delta",
            payload=AgentMessageDeltaNotification(
                delta="hello",
                item_id="item-1",
                thread_id="thread-1",
                turn_id="turn-1",
            ),
        ),
        Notification(
            method="item/reasoning/textDelta",
            payload=ReasoningTextDeltaNotification(
                content_index=0,
                delta="thinking",
                item_id="reason-1",
                thread_id="thread-1",
                turn_id="turn-1",
            ),
        ),
        Notification(
            method="item/completed",
            payload=ItemCompletedNotification(
                completed_at_ms=2,
                item=item,
                thread_id="thread-1",
                turn_id="turn-1",
            ),
        ),
        Notification(
            method="turn/completed",
            payload=TurnCompletedNotification(thread_id="thread-1", turn=turn),
        ),
    ]
    methods: list[str] = []
    for note in notifications:
        coerced = adapter._coerce_notification(note)  # pyright: ignore[reportPrivateUsage]
        assert coerced is not None
        methods.append(coerced["method"])
    assert methods == [
        "turnStarted",
        "itemStarted",
        "agentMessageDelta",
        "reasoningDelta",
        "itemCompleted",
        "turnCompleted",
    ]


def test_normalizer_reasoning_tool_and_turn_completed_variants() -> None:
    from talktoharnesses.domain.events import (
        ReasoningDeltaPayload,
        ReasoningStartedPayload,
        ToolCompletedPayload,
        ToolRequestedPayload,
        TurnCompletedPayload,
        TurnFailedPayload,
        TurnInterruptedPayload,
    )

    normalizer = CodexNormalizer()
    normalizer.set_session("t1")
    turn = uuid4()
    normalizer.begin_turn(turn)

    reasoning = normalizer.on_notification(
        {
            "method": "reasoningDelta",
            "thread_id": "t1",
            "turn_id": "u1",
            "item_id": "r1",
            "delta": "think",
        }
    )
    assert any(isinstance(e, ReasoningStartedPayload) for e in reasoning)
    assert any(isinstance(e, ReasoningDeltaPayload) for e in reasoning)

    started = normalizer.on_notification(
        {
            "method": "itemStarted",
            "thread_id": "t1",
            "turn_id": "u1",
            "item_id": "tool-1",
            "item_type": "command",
            "title": "shell",
        }
    )
    assert any(isinstance(e, ToolRequestedPayload) for e in started)
    completed = normalizer.on_notification(
        {
            "method": "itemCompleted",
            "thread_id": "t1",
            "turn_id": "u1",
            "item_id": "tool-1",
            "item_type": "command",
            "status": "failed",
        }
    )
    assert any(
        isinstance(e, ToolCompletedPayload) and e.outcome.value == "failure" for e in completed
    )

    terminal = normalizer.on_notification(
        {
            "method": "turnCompleted",
            "thread_id": "t1",
            "turn_id": "u1",
            "status": "completed",
            "final_response": "done",
            "usage": {
                "input_tokens": 1,
                "output_tokens": 2,
                "total_tokens": 3,
                "cached_input_tokens": 0,
            },
        }
    )
    assert any(isinstance(e, TurnCompletedPayload) for e in terminal)

    normalizer.begin_turn(uuid4())
    interrupted = normalizer.on_notification(
        {
            "method": "turnCompleted",
            "thread_id": "t1",
            "turn_id": "u2",
            "status": "interrupted",
        }
    )
    assert any(isinstance(e, TurnInterruptedPayload) for e in interrupted)

    normalizer.begin_turn(uuid4())
    failed = normalizer.on_notification(
        {
            "method": "turnCompleted",
            "thread_id": "t1",
            "turn_id": "u3",
            "status": "failed",
            "error_message": "boom",
        }
    )
    assert any(isinstance(e, TurnFailedPayload) for e in failed)

    # Resync / no-session / mismatch / fail_active_turn edges.
    normalizer.set_session("t1", resync=True)
    normalizer.begin_turn(uuid4())
    assert (
        normalizer.on_notification(
            {
                "method": "reasoningDelta",
                "thread_id": "t1",
                "turn_id": "u4",
                "item_id": "r2",
                "delta": "x",
            }
        )
        == []
    )
    with pytest.raises(DomainError):
        CodexNormalizer().on_notification(
            {
                "method": "agentMessageDelta",
                "thread_id": "t1",
                "turn_id": "u1",
                "item_id": "i1",
                "delta": "x",
            }
        )
    normalizer.set_session("t1", resync=False)
    normalizer.begin_turn(uuid4())
    with pytest.raises(DomainError) as mismatch:
        normalizer.on_notification(
            {
                "method": "agentMessageDelta",
                "thread_id": "other",
                "turn_id": "u1",
                "item_id": "i1",
                "delta": "x",
            }
        )
    assert mismatch.value.code is ErrorCode.PROTOCOL_ERROR
    assert normalizer.fail_active_turn(error_code="x", message="y")
    assert normalizer.fail_active_turn(error_code="x", message="y") == []


def test_approval_decision_mapping() -> None:
    adapter = CodexAdapter(client_factory=FakeCodex)
    assert adapter._to_approval_result(  # pyright: ignore[reportPrivateUsage]
        "item/commandExecution/requestApproval", ApprovalDecision.ALLOW_ONCE
    ) == {"decision": "accept"}
    assert adapter._to_approval_result(  # pyright: ignore[reportPrivateUsage]
        "item/commandExecution/requestApproval", ApprovalDecision.ALLOW_SESSION
    ) == {"decision": "acceptForSession"}
    assert adapter._to_approval_result(  # pyright: ignore[reportPrivateUsage]
        "item/commandExecution/requestApproval", ApprovalDecision.CANCEL
    ) == {"decision": "cancel"}
    assert adapter._to_approval_result(  # pyright: ignore[reportPrivateUsage]
        "item/fileChange/requestApproval", ApprovalDecision.DENY
    ) == {"decision": "decline"}


def test_normalizer_on_approval_request_command_and_file() -> None:
    from talktoharnesses.domain.events import InteractionRequestedPayload
    from talktoharnesses.providers.codex.schemas import (
        CodexCommandApprovalParams,
        CodexFileApprovalParams,
        CodexFileChangeEntry,
    )

    normalizer = CodexNormalizer()
    normalizer.set_session("t1")
    normalizer.begin_turn(uuid4())
    command = normalizer.on_approval_request(
        method="item/commandExecution/requestApproval",
        params=CodexCommandApprovalParams(command=["ls", "-la"], reason="list"),
        interaction_id=uuid4(),
    )
    assert any(isinstance(e, InteractionRequestedPayload) for e in command)
    file_events = normalizer.on_approval_request(
        method="item/fileChange/requestApproval",
        params=CodexFileApprovalParams(
            files=[CodexFileChangeEntry(path="a.py", kind="edit")],
            reason="edit",
        ),
        interaction_id=uuid4(),
    )
    assert any(isinstance(e, InteractionRequestedPayload) for e in file_events)


@pytest.mark.asyncio
async def test_redaction_seen_helpers_and_ensure_client_start_path() -> None:
    adapter = CodexAdapter(client_factory=FakeCodex)
    adapter.set_redaction_patterns(("SECRET",))
    adapter.import_seen(frozenset({"n1"}), frozenset({"o1"}))
    native, offsets = adapter.export_seen()
    assert "n1" in native and "o1" in offsets

    class _StartClient:
        def __init__(self) -> None:
            self.started = False

        async def start(self) -> None:
            self.started = True

    client = _StartClient()
    adapter._client_factory = lambda: client  # pyright: ignore[reportPrivateUsage]
    await adapter._ensure_client()  # pyright: ignore[reportPrivateUsage]
    assert client.started is True
    assert adapter._client is client  # pyright: ignore[reportPrivateUsage]


def test_codex_settings_modes_and_sandbox_wire_value() -> None:
    from openai_codex import Sandbox

    from talktoharnesses.providers.codex.adapter import (
        _codex_settings,  # pyright: ignore[reportPrivateUsage]
        _sandbox_wire_value,  # pyright: ignore[reportPrivateUsage]
    )

    _, workspace = _codex_settings("workspace-write")
    assert workspace is Sandbox.workspace_write
    _, read_only = _codex_settings("read_only")
    assert read_only is Sandbox.read_only
    _, full = _codex_settings("full-access")
    assert full is Sandbox.full_access
    with pytest.raises(DomainError) as exc:
        _codex_settings("danger-zone")
    assert exc.value.code is ErrorCode.PROVIDER_INCOMPATIBLE
    from openai_codex.generated.v2_all import SandboxMode

    assert _sandbox_wire_value(Sandbox.read_only) is SandboxMode.read_only
    assert _sandbox_wire_value("full-access") is SandboxMode.danger_full_access


@pytest.mark.asyncio
async def test_build_broker_async_codex_thread_start_and_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from talktoharnesses.providers.codex import adapter as adapter_mod

    started: list[object] = []
    resumed: list[tuple[str, object]] = []

    class _Thread:
        def __init__(self, thread_id: str) -> None:
            self.id = thread_id

    class _Started:
        def __init__(self) -> None:
            self.thread = _Thread("broker-thread")

    class FakeSyncClient:
        def __init__(self, config: object = None, approval_handler: object = None) -> None:
            self.config = config
            self.approval_handler = approval_handler

    class FakeAsyncClient:
        def __init__(self, config: object = None) -> None:
            def _approval_stub(*_a: object) -> dict[str, object]:
                return {}

            self._sync = FakeSyncClient(config=config, approval_handler=_approval_stub)

        async def thread_start(self, params: object) -> _Started:
            started.append(params)
            return _Started()

        async def thread_resume(self, thread_id: str, params: object) -> None:
            resumed.append((thread_id, params))

    class FakeAsyncCodex:
        pass

    class FakeAsyncThread:
        def __init__(self, parent: object, thread_id: str) -> None:
            self.parent = parent
            self.id = thread_id

    import openai_codex
    import openai_codex.async_client as async_client_mod
    import openai_codex.client as client_mod

    monkeypatch.setattr(openai_codex, "AsyncCodex", FakeAsyncCodex)
    monkeypatch.setattr(openai_codex, "AsyncThread", FakeAsyncThread)
    monkeypatch.setattr(async_client_mod, "AsyncCodexClient", FakeAsyncClient)
    monkeypatch.setattr(client_mod, "CodexClient", FakeSyncClient)

    def handler(method: str, params: dict[str, object] | None) -> dict[str, object]:
        del method, params
        return {"decision": "accept"}

    broker = adapter_mod._build_broker_async_codex(handler)  # pyright: ignore[reportPrivateUsage]

    async def _ensure() -> None:
        return None

    broker._ensure_initialized = _ensure  # type: ignore[method-assign]
    broker._client = FakeAsyncClient()  # type: ignore[attr-defined]
    thread = await broker.thread_start(
        cwd="/tmp", model="gpt", sandbox=SimpleNamespace(value="workspace-write")
    )
    assert thread.id == "broker-thread"
    assert started
    resumed_thread = await broker.thread_resume("broker-thread", cwd="/tmp", sandbox=None)
    assert resumed_thread.id == "broker-thread"
    assert resumed and resumed[0][0] == "broker-thread"


@pytest.mark.asyncio
async def test_approval_handler_file_deny_and_cancel_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_probe(config: HarnessConfiguration):
        from talktoharnesses.providers.codex.compatibility import match_release

        release = match_release(sdk_version="0.144.4", runtime_version="0.144.4", platform="linux")
        return release.to_harness_capabilities(), release

    monkeypatch.setattr("talktoharnesses.providers.codex.adapter.probe_codex", fake_probe)
    adapter = CodexAdapter(client_factory=FakeCodex)
    await adapter.probe(_config())
    session = await adapter.start(
        StartSessionRequest(
            conversation_id=uuid4(),
            binding_id=uuid4(),
            configuration=_config(),
            launch=_launch(),
        )
    )
    adapter._normalizer.begin_turn(uuid4())  # pyright: ignore[reportPrivateUsage]

    async def _answer(decision: ApprovalDecision) -> None:
        async for item in adapter.events(session):
            if isinstance(item, HarnessInteractionRequest):
                await adapter.answer_interaction(
                    session,
                    InteractionAnswer(
                        interaction_id=item.payload.interaction_id,
                        decision=decision,
                    ),
                )
                return

    deny_task = asyncio.create_task(_answer(ApprovalDecision.DENY))
    deny_result = await asyncio.to_thread(
        adapter._approval_handler,  # pyright: ignore[reportPrivateUsage]
        "item/fileChange/requestApproval",
        {
            "threadId": session.native_session_id,
            "turnId": "turn-1",
            "itemId": "file-1",
            "files": [{"path": "a.py", "kind": "edit"}],
            "reason": "edit",
        },
    )
    await asyncio.wait_for(deny_task, timeout=2.0)
    assert deny_result == {"decision": "decline"}

    cancel_task = asyncio.create_task(_answer(ApprovalDecision.CANCEL))
    cancel_result = await asyncio.to_thread(
        adapter._approval_handler,  # pyright: ignore[reportPrivateUsage]
        "item/commandExecution/requestApproval",
        {
            "threadId": session.native_session_id,
            "turnId": "turn-2",
            "itemId": "cmd-2",
            "command": ["rm", "-rf", "/"],
            "cwd": "/tmp",
        },
    )
    await asyncio.wait_for(cancel_task, timeout=2.0)
    assert cancel_result == {"decision": "cancel"}

    # Must run off the event-loop thread — handler blocks waiting on a Future.
    with pytest.raises(DomainError):
        await asyncio.to_thread(
            adapter._approval_handler,  # pyright: ignore[reportPrivateUsage]
            "item/unknown/requestApproval",
            {},
        )
    await adapter.close(session)


@pytest.mark.asyncio
async def test_coerce_notification_fallbacks_and_require_session() -> None:
    adapter = CodexAdapter(client_factory=FakeCodex)

    class _Dump:
        def model_dump(self) -> dict[str, object]:
            return {
                "method": "agentMessageDelta",
                "thread_id": "t",
                "turn_id": "u",
                "item_id": "i",
                "delta": "x",
            }

    coerced = adapter._coerce_notification(_Dump())  # pyright: ignore[reportPrivateUsage]
    assert coerced is not None
    assert coerced["method"] == "agentMessageDelta"

    class _Dictish:
        def __init__(self) -> None:
            self.method = "turnStarted"
            self.thread_id = "t"
            self.turn_id = "u"

    dictish = adapter._coerce_notification(_Dictish())  # pyright: ignore[reportPrivateUsage]
    assert dictish is not None
    assert dictish["method"] == "turnStarted"

    class _HookStarted:
        method = "hook/started"
        payload = None

    assert adapter._coerce_notification(_HookStarted()) is None  # pyright: ignore[reportPrivateUsage]

    with pytest.raises(DomainError) as bad_shape:
        adapter._coerce_notification(object())  # pyright: ignore[reportPrivateUsage]
    assert bad_shape.value.code is ErrorCode.UNSUPPORTED_NATIVE_EVENT

    session = HarnessSession(
        conversation_id=uuid4(),
        binding_id=uuid4(),
        kind=HarnessKind.CODEX,
        native_session_id="t1",
    )
    with pytest.raises(DomainError):
        adapter._require_session(session)  # pyright: ignore[reportPrivateUsage]
    adapter._session = session  # pyright: ignore[reportPrivateUsage]
    adapter._closed = True  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(DomainError):
        adapter._require_session(session)  # pyright: ignore[reportPrivateUsage]
    adapter._closed = False  # pyright: ignore[reportPrivateUsage]
    other = session.model_copy(update={"conversation_id": uuid4()})
    with pytest.raises(DomainError):
        adapter._require_session(other)  # pyright: ignore[reportPrivateUsage]

"""Codex adapter unit tests with a fake public SDK surface."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from uuid import uuid4

import pytest

from talktoharnesses.domain.enums import HarnessKind
from talktoharnesses.domain.events import HarnessEvent, TurnCompletedPayload
from talktoharnesses.domain.models import HarnessCapabilities, HarnessConfiguration, LaunchSnapshot
from talktoharnesses.providers.adapter import (
    HarnessInteractionRequest,
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

    async def turn(self, prompt: str) -> FakeTurnHandle:
        handle = FakeTurnHandle(
            id=f"turn-{len(self.handles) + 1}", thread_id=self.id, prompt=prompt
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
    return HarnessConfiguration(kind=HarnessKind.CODEX, working_directory="/tmp")


def _launch() -> LaunchSnapshot:
    return LaunchSnapshot(
        harness_version="0.144.4",
        working_directory="/tmp",
        adapter_version="2026.8.0.dev7",
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
    methods = [
        adapter._coerce_notification(note)["method"]  # pyright: ignore[reportPrivateUsage]
        for note in notifications
    ]
    assert methods == [
        "turnStarted",
        "itemStarted",
        "agentMessageDelta",
        "reasoningDelta",
        "itemCompleted",
        "turnCompleted",
    ]

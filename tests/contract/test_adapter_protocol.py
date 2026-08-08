"""Contract smoke for HarnessAdapter structural shape."""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest

from talktoharnesses.domain import (
    HarnessCapabilities,
    HarnessConfiguration,
    HarnessKind,
    InteractionAnswer,
    LaunchSnapshot,
)
from talktoharnesses.domain.events import HarnessEvent, TurnStartedPayload
from talktoharnesses.providers import (
    HarnessAdapter,
    HarnessSession,
    ResumeSessionRequest,
    StartSessionRequest,
    SteerRequest,
    TurnRequest,
)


def _launch() -> LaunchSnapshot:
    return LaunchSnapshot(
        resolved_executable="/bin/true",
        harness_version="test",
        working_directory="/tmp",
        adapter_version="0",
        capabilities=HarnessCapabilities(kind=HarnessKind.OPENCODE, version="test"),
    )


class RecordingAdapter:
    kind = HarnessKind.OPENCODE

    def __init__(self) -> None:
        self.closed = False

    async def probe(self, config: HarnessConfiguration) -> HarnessCapabilities:
        return HarnessCapabilities(kind=self.kind, version="test")

    async def start(self, request: StartSessionRequest) -> HarnessSession:
        return HarnessSession(
            conversation_id=request.conversation_id,
            binding_id=request.binding_id,
            kind=self.kind,
            native_session_id="n1",
        )

    async def resume(self, request: ResumeSessionRequest) -> HarnessSession:
        return HarnessSession(
            conversation_id=request.conversation_id,
            binding_id=request.binding_id,
            kind=self.kind,
            native_session_id=request.native_session_id,
        )

    async def submit(self, session: HarnessSession, request: TurnRequest) -> None:
        return None

    async def steer(self, session: HarnessSession, request: SteerRequest) -> bool:
        return True

    async def interrupt(self, session: HarnessSession) -> None:
        return None

    async def answer_interaction(self, session: HarnessSession, answer: InteractionAnswer) -> None:
        return None

    def events(self, session: HarnessSession) -> AsyncIterator[HarnessEvent]:
        async def _gen() -> AsyncIterator[HarnessEvent]:
            yield TurnStartedPayload(turn_id=uuid4())

        return _gen()

    async def close(self, session: HarnessSession) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_fake_adapter_satisfies_protocol_runtime() -> None:
    adapter: HarnessAdapter = RecordingAdapter()
    config = HarnessConfiguration(kind=HarnessKind.OPENCODE, working_directory="/tmp")
    caps = await adapter.probe(config)
    assert caps.kind is HarnessKind.OPENCODE
    launch = _launch()
    session = await adapter.start(
        StartSessionRequest(
            conversation_id=uuid4(),
            binding_id=uuid4(),
            configuration=config,
            launch=launch,
        )
    )
    assert session.native_session_id == "n1"
    await adapter.submit(session, TurnRequest(turn_id=uuid4(), prompt="hi"))
    assert await adapter.steer(session, SteerRequest(turn_id=uuid4(), prompt="more"))
    events = [e async for e in adapter.events(session)]
    assert len(events) == 1
    assert events[0].type == "turn_started"
    await adapter.close(session)

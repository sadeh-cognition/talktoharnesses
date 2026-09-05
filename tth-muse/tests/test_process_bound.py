"""Supervised-spawn path: real subprocess, process frames, forced termination."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator, Iterator
from typing import Any
from uuid import uuid4

import pytest
from django.test import AsyncClient
from tth_types.adapter import (
    HarnessInteractionRequest,
    HarnessSession,
    ResumeSessionRequest,
    StartSessionRequest,
    SteerRequest,
    TurnRequest,
)
from tth_types.enums import HarnessKind
from tth_types.events import HarnessEvent
from tth_types.harness import HarnessCapabilities, HarnessConfiguration, InteractionAnswer
from tth_types.split_api import (
    FRAME_END,
    FRAME_PROCESS,
    CreateSessionRequest,
    ProcessFrame,
    SessionCreated,
)

from tth_muse import service, sessions
from tth_muse.runtime.handle import ProcessHandle


class SpawnFakeAdapter:
    """Process-bound fake: real python child sleeps until terminated."""

    kind = HarnessKind.MUSE

    def __init__(self) -> None:
        self.handle: ProcessHandle | None = None
        self._queue: asyncio.Queue[HarnessEvent | HarnessInteractionRequest | None] = (
            asyncio.Queue()
        )

    def build_argv(self, configuration: HarnessConfiguration) -> tuple[str, ...]:
        del configuration
        return ("-c", "import time; time.sleep(300)")

    def bind_process(self, handle: ProcessHandle) -> None:
        self.handle = handle

    def export_seen(self) -> tuple[frozenset[str], frozenset[str]]:
        return frozenset(), frozenset()

    async def probe(self, config: HarnessConfiguration) -> HarnessCapabilities:
        del config
        return HarnessCapabilities(kind=HarnessKind.MUSE, version="1.0.3-R2198.1")

    async def start(self, request: StartSessionRequest) -> HarnessSession:
        return HarnessSession(
            conversation_id=request.conversation_id,
            binding_id=request.binding_id,
            kind=HarnessKind.MUSE,
            native_session_id=str(uuid4()),
        )

    async def resume(self, request: ResumeSessionRequest) -> HarnessSession:
        raise NotImplementedError

    async def submit(self, session: HarnessSession, request: TurnRequest) -> None:
        del session, request

    async def steer(self, session: HarnessSession, request: SteerRequest) -> bool:
        del session, request
        return False

    async def interrupt(self, session: HarnessSession) -> None:
        del session

    async def answer_interaction(self, session: HarnessSession, answer: InteractionAnswer) -> None:
        del session, answer

    def events(
        self, session: HarnessSession
    ) -> AsyncIterator[HarnessEvent | HarnessInteractionRequest]:
        del session

        async def _gen() -> AsyncIterator[HarnessEvent | HarnessInteractionRequest]:
            while True:
                item = await self._queue.get()
                if item is None:
                    return
                yield item

        return _gen()

    async def close(self, session: HarnessSession) -> None:
        del session
        await self._queue.put(None)


@pytest.fixture
def spawn_adapter(monkeypatch: pytest.MonkeyPatch) -> Iterator[SpawnFakeAdapter]:
    adapter = SpawnFakeAdapter()
    monkeypatch.setattr(service, "adapter_factory", lambda: adapter)
    monkeypatch.setenv("TALKTOHARNESSES_MUSE_EXECUTABLE", sys.executable)
    sessions.reset_session_store_for_tests()
    yield adapter
    sessions.reset_session_store_for_tests()


async def _read_frames(response: Any, *, until: str, limit: int = 10) -> list[tuple[str, str]]:
    frames: list[tuple[str, str]] = []
    buffer = b""
    async for chunk in response.streaming_content:
        buffer += chunk
        while b"\n\n" in buffer:
            raw, buffer = buffer.split(b"\n\n", 1)
            text = raw.decode()
            if text.startswith(":"):
                continue
            event = ""
            data = ""
            for line in text.splitlines():
                if line.startswith("event: "):
                    event = line[len("event: ") :]
                elif line.startswith("data: "):
                    data = line[len("data: ") :]
            frames.append((event, data))
            if event == until or len(frames) >= limit:
                return frames
    return frames


async def test_spawn_terminate_and_process_frames(
    spawn_adapter: SpawnFakeAdapter, tmp_path: Any
) -> None:
    client = AsyncClient()
    request = CreateSessionRequest(
        mode="start",
        conversation_id=uuid4(),
        binding_id=uuid4(),
        configuration=HarnessConfiguration(
            kind=HarnessKind.MUSE,
            working_directory=str(tmp_path),
        ),
        adapter_version="test",
    )
    response = await client.post(
        "/v1/sessions", data=request.model_dump_json(), content_type="application/json"
    )
    assert response.status_code == 201, response.content
    created = SessionCreated.model_validate_json(response.content)
    assert created.pid is not None
    assert created.launch.resolved_executable is not None
    assert spawn_adapter.handle is not None
    assert spawn_adapter.handle.pid == created.pid

    events_response = await client.get(f"/v1/sessions/{created.session_id}/events")
    assert events_response.status_code == 200

    terminate = await client.post(
        f"/v1/sessions/{created.session_id}/terminate",
        data='{"reason": "test-kill"}',
        content_type="application/json",
    )
    assert terminate.status_code == 204, terminate.content

    frames = await _read_frames(events_response, until=FRAME_END)
    names = [name for name, _ in frames]
    assert FRAME_PROCESS in names
    process_frames = [
        ProcessFrame.model_validate_json(data) for name, data in frames if name == FRAME_PROCESS
    ]
    kinds = {frame.event.type for frame in process_frames}
    assert "forced_termination" in kinds
    final = process_frames[-1].snapshot
    assert final.forced is True
    assert final.forced_reason == "test-kill"
    assert names[-1] == FRAME_END
    assert spawn_adapter.handle.returncode is not None

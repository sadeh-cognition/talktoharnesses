"""A fake public Codex SDK surface and the request helpers the adapter tests share."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from uuid import uuid4

from tth_types.enums import HarnessKind
from tth_types.harness import HarnessCapabilities, HarnessConfiguration, LaunchSnapshot


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


def harness_config() -> HarnessConfiguration:
    return HarnessConfiguration(
        kind=HarnessKind.CODEX,
        working_directory="/tmp",
        effort="high",
    )


def launch_snapshot() -> LaunchSnapshot:
    return LaunchSnapshot(
        harness_version="0.154.0",
        working_directory="/tmp",
        adapter_version="2026.8.1",
        capabilities=HarnessCapabilities(kind=HarnessKind.CODEX, version="0.154.0"),
    )

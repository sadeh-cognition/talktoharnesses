"""Shared fixtures for runtime tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from tests.runtime.helpers import child_modes_path, copy_owned_executable
from tests.runtime.memory_persistence import MemoryPersistence

from talktoharnesses.domain import (
    HarnessCapabilities,
    HarnessConfiguration,
    HarnessKind,
    InteractionAnswer,
    LaunchSnapshot,
    new_conversation_state,
)
from talktoharnesses.domain.events import HarnessEvent, TurnStartedPayload
from talktoharnesses.domain.models import ConversationHarnessBinding
from talktoharnesses.domain.transitions import ConversationState
from talktoharnesses.providers import (
    AdapterRegistry,
    HarnessSession,
    ResumeSessionRequest,
    StartSessionRequest,
    SteerRequest,
    TurnRequest,
)
from talktoharnesses.runtime import RuntimePolicy


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 8, 8, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def short_policy() -> RuntimePolicy:
    return RuntimePolicy(
        creation_timeout=2.0,
        start_resume_timeout=2.0,
        idle_reap=0.3,
        silence_warning=0.2,
        interrupt_timeout=0.3,
        graceful_close_timeout=0.3,
        terminate_escalation=0.2,
        shutdown_budget=1.0,
    )


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    d = tmp_path / "ws"
    d.mkdir()
    return d


@pytest.fixture
def owned_python(tmp_path: Path) -> Path:
    return copy_owned_executable(tmp_path / "bin")


@pytest.fixture
def child_script() -> Path:
    return child_modes_path()


def make_launch(
    *,
    executable: Path,
    workdir: Path,
    version: str = "test-1",
) -> LaunchSnapshot:
    caps = HarnessCapabilities(kind=HarnessKind.OPENCODE, version=version)
    return LaunchSnapshot(
        resolved_executable=str(executable.resolve()),
        harness_version=version,
        working_directory=str(workdir.resolve()),
        workspace_roots=(str(workdir.resolve()),),
        model="m",
        mode="default",
        adapter_version="0",
        capabilities=caps,
    )


def make_state(
    *,
    now: datetime,
    workdir: Path,
    executable: str | None = None,
    owner_id: str = "owner-1",
) -> ConversationState:
    conversation_id = uuid4()
    config = HarnessConfiguration(
        kind=HarnessKind.OPENCODE,
        executable_path=executable,
        working_directory=str(workdir),
        workspace_roots=(str(workdir),),
        model="m",
        mode="default",
    )
    binding = ConversationHarnessBinding(
        conversation_id=conversation_id,
        kind=HarnessKind.OPENCODE,
        configuration=config,
        native_session_id=None,
        created_at=now,
    )
    return new_conversation_state(
        owner_id=owner_id,
        now=now,
        binding=binding,
        conversation_id=conversation_id,
    )


class FakeAdapter:
    """Recording adapter; each factory call yields a distinct instance."""

    kind = HarnessKind.OPENCODE
    instances: list[FakeAdapter] = []

    def __init__(
        self,
        *,
        hang_start: bool = False,
        hang_interrupt: bool = False,
        hang_close: bool = False,
        start_delay: float = 0.0,
    ) -> None:
        self.hang_start = hang_start
        self.hang_interrupt = hang_interrupt
        self.hang_close = hang_close
        self.start_delay = start_delay
        self.closed = False
        self.interrupt_calls = 0
        self.instance_id = uuid4()
        FakeAdapter.instances.append(self)

    async def probe(self, config: HarnessConfiguration) -> HarnessCapabilities:
        return HarnessCapabilities(kind=self.kind, version="test-1")

    async def start(self, request: StartSessionRequest) -> HarnessSession:
        if self.hang_start:
            import asyncio

            await asyncio.sleep(3600)
        if self.start_delay:
            import asyncio

            await asyncio.sleep(self.start_delay)
        return HarnessSession(
            conversation_id=request.conversation_id,
            binding_id=request.binding_id,
            kind=self.kind,
            native_session_id=f"native-{self.instance_id.hex[:8]}",
            model=request.configuration.model,
            mode=request.configuration.mode,
        )

    async def resume(self, request: ResumeSessionRequest) -> HarnessSession:
        if self.hang_start:
            import asyncio

            await asyncio.sleep(3600)
        return HarnessSession(
            conversation_id=request.conversation_id,
            binding_id=request.binding_id,
            kind=self.kind,
            native_session_id=request.native_session_id,
        )

    async def submit(self, session: HarnessSession, request: TurnRequest) -> None:
        return None

    async def steer(self, session: HarnessSession, request: SteerRequest) -> bool:
        return False

    async def interrupt(self, session: HarnessSession) -> None:
        self.interrupt_calls += 1
        if self.hang_interrupt:
            import asyncio

            await asyncio.sleep(3600)

    async def answer_interaction(
        self,
        session: HarnessSession,
        answer: InteractionAnswer,
    ) -> None:
        return None

    def events(self, session: HarnessSession) -> AsyncIterator[HarnessEvent]:
        async def _gen() -> AsyncIterator[HarnessEvent]:
            yield TurnStartedPayload(turn_id=uuid4())

        return _gen()

    async def close(self, session: HarnessSession) -> None:
        if self.hang_close:
            import asyncio

            await asyncio.sleep(3600)
        self.closed = True


@pytest.fixture
def fake_adapter_factory() -> type[FakeAdapter]:
    FakeAdapter.instances.clear()
    return FakeAdapter


@pytest.fixture
def registry(fake_adapter_factory: type[FakeAdapter]) -> AdapterRegistry:
    reg = AdapterRegistry()
    reg.register(HarnessKind.OPENCODE, fake_adapter_factory)
    return reg


@pytest.fixture
def persistence(now: datetime, workdir: Path, owned_python: Path) -> MemoryPersistence:
    store = MemoryPersistence()
    state = make_state(now=now, workdir=workdir, executable=str(owned_python))
    store.seed(state)
    return store


def conversation_id_of(persistence: MemoryPersistence) -> UUID:
    return next(iter(persistence.states))

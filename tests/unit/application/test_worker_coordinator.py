"""Minimal WorkerCoordinator tests (SQLite singleton refuse)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import pytest
from tests.runtime.memory_persistence import MemoryPersistence

from talktoharnesses.application.command_processor import CommandProcessor
from talktoharnesses.application.worker_coordinator import WorkerCoordinator
from talktoharnesses.domain.enums import ErrorCode
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.domain.events import ConversationEvent
from talktoharnesses.providers.registry import AdapterRegistry
from talktoharnesses.runtime.manager import RuntimeManager
from talktoharnesses.runtime.policy import RuntimePolicy


def _now() -> datetime:
    return datetime.now(UTC)


class _Publisher:
    async def publish(self, events: Sequence[ConversationEvent]) -> None:
        return None

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


def _coordinator(persistence: MemoryPersistence | None = None) -> WorkerCoordinator:
    p = persistence or MemoryPersistence()
    registry = AdapterRegistry()
    publisher = _Publisher()
    runtime = RuntimeManager(p, registry, clock=_now, policy=RuntimePolicy())
    processor = CommandProcessor(p, publisher, runtime, clock=_now)  # type: ignore[arg-type]
    return WorkerCoordinator(
        p,
        runtime,
        publisher,  # type: ignore[arg-type]
        processor,
        _now,
        RuntimePolicy(),
        database_system="sqlite",
    )


@pytest.mark.asyncio
async def test_sqlite_singleton_refuses_second_worker() -> None:
    persistence = MemoryPersistence()
    first = _coordinator(persistence)
    await first.acquire_and_heartbeat("worker-a")

    second = _coordinator(persistence)
    with pytest.raises(DomainError) as exc:
        await second.acquire_and_heartbeat("worker-b")
    assert exc.value.code is ErrorCode.WORKER_LEASE_UNAVAILABLE


@pytest.mark.asyncio
async def test_same_worker_reacquire_is_idempotent() -> None:
    coordinator = _coordinator()
    await coordinator.acquire_and_heartbeat("worker-a")
    await coordinator.acquire_and_heartbeat("worker-a")
    assert coordinator.worker_id == "worker-a"
    assert coordinator.heartbeat_healthy is True


@pytest.mark.asyncio
async def test_initial_recovery_marks_ready_bits() -> None:
    coordinator = _coordinator()
    await coordinator.acquire_and_heartbeat("worker-a")
    assert coordinator.ready_for_work is False
    await coordinator.run_initial_recovery()
    assert coordinator.initial_recovery_complete is True
    assert coordinator.ready_for_work is False
    await coordinator._processor.start("worker-a")  # pyright: ignore[reportPrivateUsage]
    assert coordinator.ready_for_work is True
    snap = coordinator.readiness_snapshot()
    assert snap["recovery_complete"] is True
    assert snap["worker_lease"] is True
    await coordinator._processor.stop()  # pyright: ignore[reportPrivateUsage]

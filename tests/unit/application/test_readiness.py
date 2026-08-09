"""ReadinessProbeMonitor unit tests with an injected clock and fake adapters."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from tests.runtime.memory_persistence import MemoryPersistence

from talktoharnesses.application.readiness import PROBE_FRESHNESS, ReadinessProbeMonitor
from talktoharnesses.domain.enums import ErrorCode, HarnessKind
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.domain.models import HarnessCapabilities, HarnessConfiguration, HarnessInstance
from talktoharnesses.providers.registry import AdapterRegistry


class _Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now = self.now + delta


class _OkAdapter:
    kind = HarnessKind.GROK

    async def probe(self, config: HarnessConfiguration) -> HarnessCapabilities:
        return HarnessCapabilities(
            kind=HarnessKind.GROK,
            version="1.0.0",
            supports_steer=True,
            models=(),
            modes=(),
        )


class _FailAdapter:
    kind = HarnessKind.GROK

    async def probe(self, config: HarnessConfiguration) -> HarnessCapabilities:
        raise DomainError(ErrorCode.PROVIDER_INCOMPATIBLE, "probe failed")


def _harness(*, harness_id: UUID | None = None) -> HarnessInstance:
    return HarnessInstance(
        id=harness_id or uuid4(),
        owner_id="owner",
        name="h",
        kind=HarnessKind.GROK,
        configuration=HarnessConfiguration(kind=HarnessKind.GROK, working_directory="/tmp/ws"),
        created_at=datetime(2026, 8, 9, 12, 0, 0, tzinfo=UTC),
    )


def _caps() -> HarnessCapabilities:
    return HarnessCapabilities(
        kind=HarnessKind.GROK,
        version="1.0.0",
        supports_steer=False,
        models=(),
        modes=(),
    )


@pytest.mark.asyncio
async def test_start_probes_until_one_succeeds_in_harness_id_order() -> None:
    clock = _Clock(datetime(2026, 8, 9, 12, 0, 0, tzinfo=UTC))
    persistence = MemoryPersistence()
    early = _harness(harness_id=UUID("00000000-0000-0000-0000-000000000001"))
    late = _harness(harness_id=UUID("00000000-0000-0000-0000-000000000002"))
    await persistence.create_harness(late)
    await persistence.create_harness(early)

    probed: list[UUID] = []
    original_save = persistence.save_harness_probe

    async def _save(
        harness_id: UUID,
        owner_id: str,
        capabilities: HarnessCapabilities,
        *,
        probed_at: datetime,
    ):
        probed.append(harness_id)
        return await original_save(harness_id, owner_id, capabilities, probed_at=probed_at)

    persistence.save_harness_probe = _save  # type: ignore[method-assign]

    registry = AdapterRegistry()
    registry.register(HarnessKind.GROK, lambda: _OkAdapter())  # type: ignore[arg-type, return-value]
    monitor = ReadinessProbeMonitor(persistence, registry, clock)
    await monitor.start()
    try:
        assert probed == [early.id]
        assert monitor.is_fresh(clock())
        assert await monitor.has_fresh_probe(clock())
    finally:
        await monitor.shutdown()


@pytest.mark.asyncio
async def test_empty_registry_leaves_readiness_false() -> None:
    clock = _Clock(datetime(2026, 8, 9, 12, 0, 0, tzinfo=UTC))
    persistence = MemoryPersistence()
    registry = AdapterRegistry()
    registry.register(HarnessKind.GROK, lambda: _OkAdapter())  # type: ignore[arg-type, return-value]
    monitor = ReadinessProbeMonitor(persistence, registry, clock)
    await monitor.start()
    try:
        assert monitor.is_fresh(clock()) is False
        assert await monitor.has_fresh_probe(clock()) is False
    finally:
        await monitor.shutdown()


@pytest.mark.asyncio
async def test_failed_probes_leave_readiness_false() -> None:
    clock = _Clock(datetime(2026, 8, 9, 12, 0, 0, tzinfo=UTC))
    persistence = MemoryPersistence()
    await persistence.create_harness(_harness())
    registry = AdapterRegistry()
    registry.register(HarnessKind.GROK, lambda: _FailAdapter())  # type: ignore[arg-type, return-value]
    monitor = ReadinessProbeMonitor(persistence, registry, clock)
    await monitor.start()
    try:
        assert await monitor.has_fresh_probe(clock()) is False
    finally:
        await monitor.shutdown()


@pytest.mark.asyncio
async def test_notify_success_and_db_fallback_respect_freshness_window() -> None:
    clock = _Clock(datetime(2026, 8, 9, 12, 0, 0, tzinfo=UTC))
    persistence = MemoryPersistence()
    harness = _harness()
    await persistence.create_harness(harness)
    await persistence.save_harness_probe(
        harness.id,
        harness.owner_id,
        _caps(),
        probed_at=clock(),
    )
    registry = AdapterRegistry()
    monitor = ReadinessProbeMonitor(persistence, registry, clock)

    assert monitor.is_fresh(clock()) is False
    assert await monitor.has_fresh_probe(clock()) is True

    monitor.notify_success(clock(), harness.id)
    assert monitor.is_fresh(clock()) is True

    clock.advance(PROBE_FRESHNESS)
    assert monitor.is_fresh(clock()) is False
    assert await monitor.has_fresh_probe(clock()) is False


@pytest.mark.asyncio
async def test_refresh_retries_remaining_after_preferred_fails() -> None:
    clock = _Clock(datetime(2026, 8, 9, 12, 0, 0, tzinfo=UTC))
    persistence = MemoryPersistence()
    first = _harness(harness_id=UUID("00000000-0000-0000-0000-000000000001"))
    second = _harness(harness_id=UUID("00000000-0000-0000-0000-000000000002"))
    await persistence.create_harness(first)
    await persistence.create_harness(second)

    registry = AdapterRegistry()
    registry.register(HarnessKind.GROK, lambda: _OkAdapter())  # type: ignore[arg-type, return-value]
    monitor = ReadinessProbeMonitor(persistence, registry, clock)

    probed: list[UUID] = []
    fail_first_on_refresh = False

    async def _probe_one(harness: HarnessInstance) -> bool:
        probed.append(harness.id)
        if fail_first_on_refresh and harness.id == first.id:
            return False
        monitor.notify_success(clock(), harness.id)
        return True

    monitor._probe_one = _probe_one  # type: ignore[method-assign]
    await monitor.start()
    assert monitor._successful_harness_id == first.id  # type: ignore[attr-defined]

    probed.clear()
    fail_first_on_refresh = True
    await monitor._refresh_cycle()  # type: ignore[attr-defined]
    assert probed[0] == first.id
    assert second.id in probed
    assert monitor._successful_harness_id == second.id  # type: ignore[attr-defined]
    await monitor.shutdown()

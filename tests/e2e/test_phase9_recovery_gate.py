"""Phase 9 recovery gate essentials (memory persistence + fake adapter)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from tests.runtime.conftest import FakeAdapter
from tests.runtime.memory_persistence import MemoryPersistence

from talktoharnesses.application.broker import InProcessCommittedEventBroker
from talktoharnesses.application.service import TalkToHarnessesService
from talktoharnesses.domain.enums import (
    CommandKind,
    CommandStatus,
    ConversationStatus,
    HarnessKind,
    TurnStatus,
)
from talktoharnesses.domain.models import HarnessConfiguration
from talktoharnesses.domain.transitions import start_turn
from talktoharnesses.providers.registry import AdapterRegistry
from talktoharnesses.runtime.manager import RuntimeManager
from talktoharnesses.runtime.policy import RuntimePolicy


def _now() -> datetime:
    return datetime(2026, 8, 9, 14, 0, 0, tzinfo=UTC)


class _GateAdapter(FakeAdapter):
    sdk_managed = True
    kind = HarnessKind.GROK


@pytest.mark.asyncio
async def test_phase9_idle_no_runtime_and_ambiguous_never_redelivers() -> None:
    FakeAdapter.instances.clear()
    persistence = MemoryPersistence()
    registry = AdapterRegistry()
    registry.register(HarnessKind.GROK, _GateAdapter)  # type: ignore[arg-type]
    broker = InProcessCommittedEventBroker(poll_interval=10.0, keepalive_interval=30.0)
    runtime = RuntimeManager(
        persistence,
        registry,
        clock=_now,
        policy=RuntimePolicy(lease_duration=30.0, lease_renewal_interval=60.0),
    )
    service = TalkToHarnessesService(persistence, registry, broker, _now, runtime)
    await broker.start()

    try:
        harness = await service.create_harness(
            "owner",
            name="gate",
            configuration=HarnessConfiguration(
                kind=HarnessKind.GROK,
                working_directory="/tmp",
            ),
        )
        snap = await service.create_conversation("owner", harness.id, title="idle")
        cid = snap.detail.conversation.id

        # Idle conversations must not create a runtime until a command needs one.
        assert runtime.get_runtime(cid) is None

        submitted = await service.submit_turn(
            "owner",
            cid,
            prompt="recover-me",
            idempotency_key="p9-gate-1",
        )
        assert submitted.command.status is CommandStatus.ACCEPTED
        assert runtime.get_runtime(cid) is None

        # Simulate crash after delivery_started: active turn + ambiguous command.
        state = await persistence.get_worker_snapshot(cid)
        started = start_turn(state, now=_now())
        command = next(
            c
            for c in started.state.commands.values()
            if c.kind is CommandKind.SUBMIT_TURN and c.id == submitted.command.id
        )
        ambiguous = command.model_copy(
            update={
                "status": CommandStatus.DELIVERY_STARTED,
                "delivery_started_at": _now(),
                "worker_id": "dead-worker",
                "lease_expires_at": _now() - timedelta(seconds=1),
                "attempts": 1,
            }
        )
        commands = dict(started.state.commands)
        commands[ambiguous.id] = ambiguous
        next_state = started.state.model_copy(
            update={
                "commands": commands,
                "conversation": started.state.conversation.model_copy(
                    update={"status": ConversationStatus.RUNNING}
                ),
            }
        )
        await persistence.save_snapshot(next_state)
        persistence.commands[ambiguous.id] = ambiguous
        if ambiguous.id in persistence.accepted_queue:
            persistence.accepted_queue.remove(ambiguous.id)

        # Expired/unowned active conversation is claimed by startup recovery.
        await service.coordinator.acquire_and_heartbeat("e2e-p9")
        await service.coordinator.run_initial_recovery()

        recovered = await persistence.get_worker_snapshot(cid)
        stored = recovered.commands[ambiguous.id]
        assert stored.status is CommandStatus.OUTCOME_UNKNOWN
        assert (
            recovered.active_turn is None
            or recovered.active_turn.status is not TurnStatus.RUNNING
        )

        # Claims must never re-deliver an outcome_unknown command.
        service.processor.set_claims_enabled(True)
        await service.processor.start("e2e-p9")
        await asyncio.sleep(0.05)
        await service.processor.stop()

        assert all(not getattr(a, "submissions", None) for a in FakeAdapter.instances) or (
            sum(len(a.submissions) for a in FakeAdapter.instances) == 0
        )
        assert stored.status is CommandStatus.OUTCOME_UNKNOWN
        final = persistence.commands[ambiguous.id]
        assert final.status is CommandStatus.OUTCOME_UNKNOWN
    finally:
        await service.coordinator.begin_shutdown(asyncio.get_running_loop().time() + 2)
        await service.coordinator.finish_shutdown()
        await broker.stop()

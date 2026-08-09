"""Unit barriers for Phase 9 WP6 fault-injection checkpoints."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from tests.runtime.memory_persistence import MemoryPersistence

from talktoharnesses.application.command_processor import CommandProcessor
from talktoharnesses.application.faults import FaultPoint
from talktoharnesses.domain import (
    CommandStatus,
    HarnessCapabilities,
    HarnessConfiguration,
    HarnessKind,
    append_events,
    new_conversation_state,
    submit_turn,
)
from talktoharnesses.domain.events import (
    ConversationEvent,
    HarnessEvent,
    ProviderWarningPayload,
    TurnCompletedPayload,
)
from talktoharnesses.domain.models import ConversationHarnessBinding
from talktoharnesses.providers.adapter import HarnessSession, TurnRequest


class _Publisher:
    def __init__(self) -> None:
        self.events: list[ConversationEvent] = []

    async def publish(self, events: Sequence[ConversationEvent]) -> None:
        self.events.extend(events)


class _Adapter:
    def __init__(self) -> None:
        self.submissions: list[TurnRequest] = []

    async def submit(self, session: HarnessSession, request: TurnRequest) -> None:
        self.submissions.append(request)

    def events(self, session: HarnessSession) -> AsyncIterator[HarnessEvent]:
        async def gen() -> AsyncIterator[HarnessEvent]:
            while not self.submissions:
                await asyncio.sleep(0)
            yield TurnCompletedPayload(
                turn_id=self.submissions[0].turn_id,
                terminal_reason="end_turn",
            )

        return gen()


class _Runtime:
    def __init__(self, persistence: MemoryPersistence, adapter: _Adapter) -> None:
        self.persistence = persistence
        self.adapter = adapter
        self.managed = None

    def get_runtime(self, conversation_id: UUID):
        return self.managed

    async def ensure_binding_current(self, conversation_id: UUID, state: Any):
        return self.managed

    async def start(
        self,
        *,
        conversation_id: UUID,
        owner_id: str,
        **kwargs: Any,
    ) -> HarnessSession:
        state = await self.persistence.get_worker_snapshot(conversation_id)
        assert state.binding is not None
        binding = state.binding.model_copy(update={"native_session_id": "session-1"})
        state = state.model_copy(update={"binding": binding})
        next_state, events = append_events(
            state,
            datetime.now(UTC),
            [ProviderWarningPayload(message="runtime started")],
        )
        await self.persistence.commit_runtime_lifecycle(
            conversation_id,
            state.conversation.version,
            next_state,
            None,
            None,
            events,
        )
        session = HarnessSession(
            conversation_id=conversation_id,
            binding_id=binding.id,
            kind=HarnessKind.GROK,
            native_session_id="session-1",
        )
        self.managed = SimpleNamespace(adapter=self.adapter, session=session)
        return session

    async def resume(self, **kwargs: Any) -> HarnessSession:
        return await self.start(**kwargs)

    async def close(self, conversation_id: UUID, *, reason: str) -> None:
        self.managed = None


@pytest.mark.asyncio
async def test_submit_checkpoint_order_claim_to_delivered() -> None:
    now = datetime(2026, 8, 9, tzinfo=UTC)
    state = new_conversation_state(
        owner_id="owner",
        now=now,
        capabilities=HarnessCapabilities(
            kind=HarnessKind.GROK,
            version="1.0.0",
            supports_resume=True,
        ),
    )
    binding = ConversationHarnessBinding(
        conversation_id=state.conversation.id,
        kind=HarnessKind.GROK,
        configuration=HarnessConfiguration(kind=HarnessKind.GROK, working_directory="/tmp"),
        created_at=now,
    )
    state = state.model_copy(
        update={
            "binding": binding,
            "conversation": state.conversation.model_copy(
                update={"current_binding_id": binding.id}
            ),
        }
    )
    submitted = submit_turn(state, prompt="hello", idempotency_key="k1", now=now)
    assert submitted.command is not None

    persistence = MemoryPersistence()
    persistence.seed(submitted.state)
    await persistence.accept_command(submitted.command)

    seen: list[FaultPoint] = []

    async def _cb(point: FaultPoint) -> None:
        seen.append(point)

    adapter = _Adapter()
    runtime = _Runtime(persistence, adapter)
    processor = CommandProcessor(
        persistence,
        _Publisher(),
        runtime,  # type: ignore[arg-type]
        clock=lambda: now,
        poll_interval=0.001,
        fault_callback=_cb,
    )

    await processor.start("worker-1")
    expected = [
        FaultPoint.AFTER_CLAIM_COMMIT,
        FaultPoint.AFTER_DELIVERY_STARTED,
        FaultPoint.AFTER_NATIVE_ACK,
        FaultPoint.AFTER_DELIVERED,
    ]
    for _ in range(200):
        if all(point in seen for point in expected):
            break
        await asyncio.sleep(0.01)
    await processor.stop()

    assert adapter.submissions
    assert persistence.commands[submitted.command.id].status in {
        CommandStatus.DELIVERED,
        CommandStatus.SETTLED,
    }
    positions = [seen.index(point) for point in expected]
    assert positions == sorted(positions)

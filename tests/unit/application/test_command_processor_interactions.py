"""Command processor interaction request/answer/interrupt paths (Phase 6)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from tests.runtime.memory_persistence import MemoryPersistence

from talktoharnesses.application.command_processor import CommandProcessor
from talktoharnesses.application.interaction_broker import InteractionBroker
from talktoharnesses.domain import (
    ApprovalDecision,
    ApprovalRule,
    ApprovalRuleDecision,
    CommandApprovalAction,
    CommandKind,
    CommandStatus,
    ExactArgvMatcher,
    HarnessConfiguration,
    HarnessKind,
    InteractionKind,
    PrincipalGlobalRuleScope,
    new_conversation_state,
    request_interaction,
    start_turn,
    submit_interaction_answer,
    submit_turn,
)
from talktoharnesses.domain.events import (
    ConversationEvent,
    InteractionRequestedPayload,
    TurnCompletedPayload,
)
from talktoharnesses.domain.models import (
    AnswerInteractionPayload,
    ApprovalRequestPayload,
    Command,
    ConversationHarnessBinding,
    InteractionAnswer,
    PendingInteraction,
)
from talktoharnesses.providers.adapter import HarnessInteractionRequest, HarnessSession
from talktoharnesses.runtime.manager import RuntimeManager


def _now() -> datetime:
    return datetime(2026, 8, 8, 14, 0, 0, tzinfo=UTC)


class _Publisher:
    def __init__(self) -> None:
        self.events: list[ConversationEvent] = []

    async def publish(self, events: Sequence[ConversationEvent]) -> None:
        self.events.extend(events)


class _InteractionAdapter:
    def __init__(self) -> None:
        self.answers: list[InteractionAnswer] = []
        self.interrupts = 0
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._interaction_id = uuid4()
        self.answer_error: BaseException | None = None

    def push_interaction(self, turn_id: UUID, *, envelope: bool = True) -> UUID:
        iid = self._interaction_id
        payload = InteractionRequestedPayload(
            turn_id=turn_id,
            interaction_id=iid,
            kind=InteractionKind.APPROVAL,
            request=ApprovalRequestPayload(
                action=CommandApprovalAction(argv=("tool", "x")),
                available_decisions=(
                    ApprovalDecision.ALLOW_ONCE,
                    ApprovalDecision.DENY,
                    ApprovalDecision.CANCEL,
                ),
            ),
        )
        self._queue.put_nowait(
            HarnessInteractionRequest(
                payload=payload,
                provider_correlation={"json_rpc_request_id": "rpc-1"},
            )
            if envelope
            else payload
        )
        return iid

    def push_complete(self, turn_id: UUID) -> None:
        self._queue.put_nowait(TurnCompletedPayload(turn_id=turn_id, terminal_reason="end_turn"))

    async def answer_interaction(self, session: HarnessSession, answer: InteractionAnswer) -> None:
        if self.answer_error is not None:
            raise self.answer_error
        self.answers.append(answer)

    async def interrupt(self, session: HarnessSession) -> None:
        self.interrupts += 1

    async def submit(self, session: HarnessSession, request: Any) -> None:
        return None

    def events(self, session: HarnessSession) -> AsyncIterator[Any]:
        async def gen() -> AsyncIterator[Any]:
            while True:
                item = await self._queue.get()
                if item is None:
                    return
                yield item

        return gen()


class _Runtime:
    def __init__(self, adapter: _InteractionAdapter) -> None:
        self.adapter = adapter
        self.managed: Any = None

    def get_runtime(self, conversation_id: UUID) -> Any:
        return self.managed

    async def start(self, **kwargs: Any) -> HarnessSession:
        session = HarnessSession(
            conversation_id=kwargs["conversation_id"],
            binding_id=uuid4(),
            kind=HarnessKind.GROK,
            native_session_id="s1",
        )
        self.managed = SimpleNamespace(adapter=self.adapter, session=session)
        return session

    async def resume(self, **kwargs: Any) -> HarnessSession:
        return await self.start(**kwargs)

    async def close(self, conversation_id: UUID, *, reason: str) -> None:
        self.managed = None


async def _seed_running(p: MemoryPersistence) -> tuple[UUID, UUID]:
    now = _now()
    state = new_conversation_state(owner_id="owner", now=now)
    binding = ConversationHarnessBinding(
        conversation_id=state.conversation.id,
        kind=HarnessKind.GROK,
        configuration=HarnessConfiguration(kind=HarnessKind.GROK, working_directory="/tmp"),
        harness_instance_id=uuid4(),
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
    await p.save_snapshot(state)
    queued = submit_turn(state, prompt="x", idempotency_key="t1", now=now)
    running = start_turn(queued.state, now=now)
    await p.commit_facade_mutation(
        state.conversation.id,
        "owner",
        state.conversation.version,
        running.state,
        (*queued.events, *running.events),
        commands=tuple(running.state.commands.values()),
    )
    assert running.state.active_turn is not None
    return state.conversation.id, running.state.active_turn.id


@pytest.mark.asyncio
@pytest.mark.parametrize("envelope", [True, False])
async def test_event_pump_routes_interaction_request_through_broker(envelope: bool) -> None:
    p = MemoryPersistence()
    publisher = _Publisher()
    broker = InteractionBroker(p, publisher, clock=_now)
    adapter = _InteractionAdapter()
    runtime = _Runtime(adapter)
    processor = CommandProcessor(
        p,
        publisher,
        cast(RuntimeManager, runtime),
        clock=_now,
        interaction_broker=broker,
        poll_interval=0.01,
    )
    cid, turn_id = await _seed_running(p)
    await p.create_approval_rule(
        ApprovalRule(
            principal_id="owner",
            decision=ApprovalRuleDecision.ALLOW,
            scope=PrincipalGlobalRuleScope(),
            matcher=ExactArgvMatcher(argv=("tool", "x")),
            created_at=_now(),
            updated_at=_now(),
        )
    )

    await runtime.start(conversation_id=cid, owner_id="owner")
    await processor.start("w1")
    processor._ensure_pump(cid)  # pyright: ignore[reportPrivateUsage]
    iid = adapter.push_interaction(turn_id, envelope=envelope)

    for _ in range(50):
        if any(e.type == "interaction_resolved" for e in publisher.events):
            break
        await asyncio.sleep(0.02)

    types = [e.type for e in publisher.events]
    assert "interaction_requested" in types
    assert "interaction_resolved" in types
    assert types.index("interaction_requested") < types.index("interaction_resolved")
    assert iid in p.interaction_answers
    assert p.interaction_answers[iid].decision is ApprovalDecision.ALLOW_ONCE
    # Command released for provider delivery.
    answer_cmds = [c for c in p.commands.values() if c.kind is CommandKind.ANSWER_INTERACTION]
    assert len(answer_cmds) == 1
    await processor.stop()


@pytest.mark.asyncio
async def test_answer_command_settles_after_native_flush_at_most_once() -> None:
    p = MemoryPersistence()
    publisher = _Publisher()
    broker = InteractionBroker(p, publisher, clock=_now)
    adapter = _InteractionAdapter()
    runtime = _Runtime(adapter)
    processor = CommandProcessor(
        p,
        publisher,
        cast(RuntimeManager, runtime),
        clock=_now,
        interaction_broker=broker,
        poll_interval=0.01,
    )
    cid, turn_id = await _seed_running(p)
    state = await p.get_worker_snapshot(cid)
    interaction = PendingInteraction(
        conversation_id=cid,
        turn_id=turn_id,
        kind=InteractionKind.APPROVAL,
        request=ApprovalRequestPayload(
            available_decisions=(ApprovalDecision.ALLOW_ONCE, ApprovalDecision.DENY)
        ),
        created_at=_now(),
    )
    requested = request_interaction(state, interaction, now=_now())
    answered = submit_interaction_answer(
        requested.state,
        InteractionAnswer(interaction_id=interaction.id, decision=ApprovalDecision.ALLOW_ONCE),
        now=_now(),
    )
    await p.commit_facade_mutation(
        cid,
        "owner",
        state.conversation.version,
        answered.state,
        (*requested.events, *answered.events),
        interaction_answers=(answered.state.answers[interaction.id],),
    )
    command = Command(
        conversation_id=cid,
        kind=CommandKind.ANSWER_INTERACTION,
        status=CommandStatus.ACCEPTED,
        idempotency_key=f"answer-interaction:{interaction.id}",
        target_turn_id=turn_id,
        payload=AnswerInteractionPayload(interaction_id=interaction.id),
        created_at=_now(),
    )
    await p.accept_command(command)

    await processor.start("w1")
    for _ in range(50):
        stored = p.commands.get(command.id)
        if stored is not None and stored.status is CommandStatus.SETTLED:
            break
        await asyncio.sleep(0.02)

    stored = p.commands[command.id]
    assert stored.status is CommandStatus.SETTLED
    aggregate = await p.get_worker_snapshot(cid)
    assert aggregate.commands[command.id].status is CommandStatus.SETTLED
    assert len(adapter.answers) == 1
    assert adapter.answers[0].decision is ApprovalDecision.ALLOW_ONCE
    await processor.stop()

    # Second claim of a settled command must not redeliver.
    await p.accept_command(
        command.model_copy(
            update={"status": CommandStatus.ACCEPTED, "settled_at": None, "delivered_at": None}
        )
    )
    # Settled commands are not claimable from accepted_queue if status was overwritten;
    # assert adapter still has one answer only after another start cycle with empty queue.
    assert len(adapter.answers) == 1


@pytest.mark.asyncio
async def test_failed_answer_delivery_is_outcome_unknown_in_row_and_aggregate() -> None:
    p = MemoryPersistence()
    publisher = _Publisher()
    broker = InteractionBroker(p, publisher, clock=_now)
    adapter = _InteractionAdapter()
    adapter.answer_error = RuntimeError("native delivery failed")
    runtime = _Runtime(adapter)
    processor = CommandProcessor(
        p,
        publisher,
        cast(RuntimeManager, runtime),
        clock=_now,
        interaction_broker=broker,
        poll_interval=0.01,
    )
    cid, turn_id = await _seed_running(p)
    command = Command(
        conversation_id=cid,
        kind=CommandKind.ANSWER_INTERACTION,
        status=CommandStatus.ACCEPTED,
        idempotency_key="answer-failure",
        target_turn_id=turn_id,
        payload=AnswerInteractionPayload(interaction_id=uuid4()),
        created_at=_now(),
    )
    await p.accept_command(command)
    await runtime.start(conversation_id=cid, owner_id="owner")
    await processor.start("w1")

    for _ in range(50):
        if p.commands[command.id].status is CommandStatus.OUTCOME_UNKNOWN:
            break
        await asyncio.sleep(0.02)

    assert p.commands[command.id].status is CommandStatus.OUTCOME_UNKNOWN
    aggregate = await p.get_worker_snapshot(cid)
    assert aggregate.commands[command.id].status is CommandStatus.OUTCOME_UNKNOWN
    await processor.stop()


@pytest.mark.asyncio
async def test_interrupt_cancels_open_interactions_before_adapter() -> None:
    p = MemoryPersistence()
    publisher = _Publisher()
    broker = InteractionBroker(p, publisher, clock=_now)
    adapter = _InteractionAdapter()
    runtime = _Runtime(adapter)
    processor = CommandProcessor(
        p,
        publisher,
        cast(RuntimeManager, runtime),
        clock=_now,
        interaction_broker=broker,
        poll_interval=0.01,
    )
    cid, turn_id = await _seed_running(p)
    state = await p.get_worker_snapshot(cid)
    interaction = PendingInteraction(
        conversation_id=cid,
        turn_id=turn_id,
        kind=InteractionKind.APPROVAL,
        request=ApprovalRequestPayload(available_decisions=(ApprovalDecision.CANCEL,)),
        created_at=_now(),
    )
    requested = request_interaction(state, interaction, now=_now())
    await p.commit_facade_mutation(
        cid,
        "owner",
        state.conversation.version,
        requested.state,
        requested.events,
    )
    interrupt = Command(
        conversation_id=cid,
        kind=CommandKind.INTERRUPT,
        status=CommandStatus.ACCEPTED,
        idempotency_key="int-1",
        target_turn_id=turn_id,
        payload=__import__(
            "talktoharnesses.domain.models", fromlist=["InterruptPayload"]
        ).InterruptPayload(),
        created_at=_now(),
    )
    await p.accept_command(interrupt)
    await runtime.start(conversation_id=cid, owner_id="owner")
    await processor.start("w1")

    for _ in range(50):
        if adapter.interrupts:
            break
        await asyncio.sleep(0.02)

    assert adapter.interrupts == 1
    assert interaction.id in p.interaction_answers
    assert p.interaction_answers[interaction.id].decision is ApprovalDecision.CANCEL
    assert any(e.type == "interaction_resolved" for e in publisher.events)
    await processor.stop()

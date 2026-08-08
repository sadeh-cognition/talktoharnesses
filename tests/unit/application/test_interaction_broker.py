"""InteractionBroker: auto policy, publication order, reconciliation."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

import pytest
from tests.runtime.memory_persistence import MemoryPersistence

from talktoharnesses.application.interaction_broker import InteractionBroker
from talktoharnesses.application.service import TalkToHarnessesService
from talktoharnesses.domain import (
    ApprovalDecision,
    ApprovalRule,
    ApprovalRuleDecision,
    CommandApprovalAction,
    ExactArgvMatcher,
    HarnessConfiguration,
    HarnessKind,
    InteractionKind,
    PrincipalGlobalRuleScope,
)
from talktoharnesses.domain.events import ConversationEvent
from talktoharnesses.domain.models import ApprovalRequestPayload, PendingInteraction
from talktoharnesses.domain.transitions import request_interaction, start_turn, submit_turn
from talktoharnesses.providers.registry import AdapterRegistry
from talktoharnesses.runtime.manager import RuntimeManager


def _now() -> datetime:
    return datetime(2026, 8, 8, 15, 30, 0, tzinfo=UTC)


class _Publisher:
    def __init__(self) -> None:
        self.events: list[ConversationEvent] = []
        self.fail_on: set[str] = set()
        self.publish_calls = 0

    async def publish(self, events: Sequence[ConversationEvent]) -> None:
        self.publish_calls += 1
        for event in events:
            if event.type in self.fail_on:
                raise RuntimeError(f"publisher fail on {event.type}")
        self.events.extend(events)

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


class _NoReplayPersistence(MemoryPersistence):
    async def replay(
        self,
        conversation_id: UUID,
        after_sequence: int,
        event_count_limit: int,
        byte_limit: int,
    ) -> Sequence[ConversationEvent]:
        raise AssertionError("recovery must load the exact request event")


async def _seed_running_turn(
    p: MemoryPersistence, owner: str = "owner"
) -> tuple[TalkToHarnessesService, MemoryPersistence, UUID]:
    from talktoharnesses.domain import HarnessCapabilities

    registry = AdapterRegistry()
    runtime = RuntimeManager(p, registry, clock=_now)
    service = TalkToHarnessesService(p, registry, _Publisher(), _now, runtime)
    config = HarnessConfiguration(kind=HarnessKind.GROK, working_directory="/tmp/ws")
    harness = await service.create_harness(owner, name="h", configuration=config)
    # Seed capabilities so conversations are normal.
    await p.save_harness_probe(
        harness.id,
        owner,
        HarnessCapabilities(kind=HarnessKind.GROK, version="1.0.0"),
        probed_at=_now(),
    )
    snap = await service.create_conversation(owner, harness.id)
    cid = snap.detail.conversation.id
    state = await p.get_snapshot(cid, owner)
    queued = submit_turn(state, prompt="x", idempotency_key="t1", now=_now())
    running = start_turn(queued.state, now=_now())
    await p.commit_facade_mutation(
        cid,
        owner,
        state.conversation.version,
        running.state,
        (*queued.events, *running.events),
        commands=tuple(running.state.commands.values()),
    )
    return service, p, cid


def _interaction(
    cid: UUID,
    turn_id: UUID,
    *,
    argv: tuple[str, ...] = ("tool", "a"),
) -> PendingInteraction:
    return PendingInteraction(
        conversation_id=cid,
        turn_id=turn_id,
        kind=InteractionKind.APPROVAL,
        request=ApprovalRequestPayload(
            action=CommandApprovalAction(argv=argv),
            available_decisions=(
                ApprovalDecision.ALLOW_ONCE,
                ApprovalDecision.DENY,
                ApprovalDecision.CANCEL,
            ),
        ),
        created_at=_now(),
    )


@pytest.mark.asyncio
async def test_accept_request_publishes_before_auto_resolution() -> None:
    p = MemoryPersistence()
    publisher = _Publisher()
    _service, p, cid = await _seed_running_turn(p)
    broker = InteractionBroker(p, publisher, clock=_now)
    state = await p.get_worker_snapshot(cid)  # type: ignore[arg-type]
    turn_id = state.active_turn.id  # type: ignore[union-attr]
    rule = ApprovalRule(
        principal_id="owner",
        decision=ApprovalRuleDecision.ALLOW,
        scope=PrincipalGlobalRuleScope(),
        matcher=ExactArgvMatcher(argv=("tool", "a")),
        created_at=_now(),
        updated_at=_now(),
    )
    await p.create_approval_rule(rule)
    interaction = _interaction(cid, turn_id)

    await broker.accept_request(
        cid,  # type: ignore[arg-type]
        interaction,
        provider_correlation={"json_rpc_request_id": "1"},
    )

    types = [e.type for e in publisher.events]
    assert "interaction_requested" in types
    assert "interaction_resolved" in types
    assert types.index("interaction_requested") < types.index("interaction_resolved")
    # Resolution is automatic allow_once.
    resolved = next(e for e in publisher.events if e.type == "interaction_resolved")
    assert resolved.payload.automatic is True  # type: ignore[attr-defined]
    assert resolved.payload.decision is ApprovalDecision.ALLOW_ONCE  # type: ignore[attr-defined]
    # Answer command released after publish.
    commands = [c for c in p.commands.values() if c.kind.value == "answer_interaction"]
    assert len(commands) == 1
    assert p.interaction_meta[interaction.id].get("released_at") is not None


@pytest.mark.asyncio
async def test_accept_request_deny_rule_wins() -> None:
    p = MemoryPersistence()
    publisher = _Publisher()
    await _seed_running_turn(p)
    broker = InteractionBroker(p, publisher, clock=_now)
    cid = next(iter(p.states))
    state = await p.get_worker_snapshot(cid)
    turn_id = state.active_turn.id  # type: ignore[union-attr]
    await p.create_approval_rule(
        ApprovalRule(
            principal_id="owner",
            decision=ApprovalRuleDecision.DENY,
            scope=PrincipalGlobalRuleScope(),
            matcher=ExactArgvMatcher(argv=("rm", "-rf", "/")),
            created_at=_now(),
            updated_at=_now(),
        )
    )
    interaction = _interaction(cid, turn_id, argv=("rm", "-rf", "/"))
    await broker.accept_request(cid, interaction)
    resolved = next(e for e in publisher.events if e.type == "interaction_resolved")
    assert resolved.payload.decision is ApprovalDecision.DENY  # type: ignore[attr-defined]
    assert resolved.payload.automatic is True  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_publisher_failure_on_request_leaves_unevaluated() -> None:
    p = MemoryPersistence()
    publisher = _Publisher()
    publisher.fail_on.add("interaction_requested")
    await _seed_running_turn(p)
    broker = InteractionBroker(p, publisher, clock=_now)
    cid = next(iter(p.states))
    state = await p.get_worker_snapshot(cid)
    turn_id = state.active_turn.id  # type: ignore[union-attr]
    await p.create_approval_rule(
        ApprovalRule(
            principal_id="owner",
            decision=ApprovalRuleDecision.ALLOW,
            scope=PrincipalGlobalRuleScope(),
            matcher=ExactArgvMatcher(argv=("tool", "a")),
            created_at=_now(),
            updated_at=_now(),
        )
    )
    interaction = _interaction(cid, turn_id)
    await broker.accept_request(cid, interaction)
    # Request committed but policy not evaluated.
    assert p.interaction_meta[interaction.id].get("policy_evaluated_at") is None
    assert not any(e.type == "interaction_resolved" for e in publisher.events)
    # Still open for manual answer.
    assert interaction.id in (await p.get_worker_snapshot(cid)).interactions


@pytest.mark.asyncio
async def test_duplicate_request_republishes_before_policy_evaluation() -> None:
    p = MemoryPersistence()
    publisher = _Publisher()
    publisher.fail_on.add("interaction_requested")
    await _seed_running_turn(p)
    broker = InteractionBroker(p, publisher, clock=_now)
    cid = next(iter(p.states))
    state = await p.get_worker_snapshot(cid)
    assert state.active_turn is not None
    interaction = _interaction(cid, state.active_turn.id)
    await p.create_approval_rule(
        ApprovalRule(
            principal_id="owner",
            decision=ApprovalRuleDecision.ALLOW,
            scope=PrincipalGlobalRuleScope(),
            matcher=ExactArgvMatcher(argv=("tool", "a")),
            created_at=_now(),
            updated_at=_now(),
        )
    )

    await broker.accept_request(cid, interaction)
    publisher.fail_on.clear()
    await broker.accept_request(cid, interaction)

    assert [event.type for event in publisher.events] == [
        "interaction_requested",
        "interaction_resolved",
    ]


@pytest.mark.asyncio
async def test_duplicate_evaluated_no_match_is_not_re_evaluated() -> None:
    p = MemoryPersistence()
    publisher = _Publisher()
    await _seed_running_turn(p)
    broker = InteractionBroker(p, publisher, clock=_now)
    cid = next(iter(p.states))
    state = await p.get_worker_snapshot(cid)
    assert state.active_turn is not None
    interaction = _interaction(cid, state.active_turn.id)
    await broker.accept_request(cid, interaction)
    published_before_retry = tuple(publisher.events)
    await p.create_approval_rule(
        ApprovalRule(
            principal_id="owner",
            decision=ApprovalRuleDecision.ALLOW,
            scope=PrincipalGlobalRuleScope(),
            matcher=ExactArgvMatcher(argv=("tool", "a")),
            created_at=_now(),
            updated_at=_now(),
        )
    )

    await broker.accept_request(cid, interaction)

    assert tuple(publisher.events) == published_before_retry
    assert not any(event.type == "interaction_resolved" for event in publisher.events)
    assert interaction.id not in p.interaction_answers


@pytest.mark.asyncio
async def test_reconcile_loads_exact_stored_request_event() -> None:
    p = _NoReplayPersistence()
    publisher = _Publisher()
    publisher.fail_on.add("interaction_requested")
    await _seed_running_turn(p)
    cid = next(iter(p.states))
    state = await p.get_worker_snapshot(cid)
    assert state.active_turn is not None
    interaction = _interaction(cid, state.active_turn.id)
    await InteractionBroker(p, publisher, clock=_now).accept_request(cid, interaction)

    healthy = _Publisher()
    await InteractionBroker(p, healthy, clock=_now).reconcile_on_startup()

    assert [event.type for event in healthy.events] == ["interaction_requested"]


@pytest.mark.asyncio
async def test_no_match_marks_evaluated_without_resolution() -> None:
    p = MemoryPersistence()
    publisher = _Publisher()
    await _seed_running_turn(p)
    broker = InteractionBroker(p, publisher, clock=_now)
    cid = next(iter(p.states))
    state = await p.get_worker_snapshot(cid)
    turn_id = state.active_turn.id  # type: ignore[union-attr]
    interaction = _interaction(cid, turn_id)
    await broker.accept_request(cid, interaction)
    assert any(e.type == "interaction_requested" for e in publisher.events)
    assert not any(e.type == "interaction_resolved" for e in publisher.events)
    assert p.interaction_meta[interaction.id].get("policy_evaluated_at") is not None


@pytest.mark.asyncio
async def test_reconcile_releases_unreleased_after_publish_failure() -> None:
    p = MemoryPersistence()
    _svc, p, cid = await _seed_running_turn(p)
    # Use service broker with failing publisher for first resolve.
    fail_pub = _Publisher()
    fail_pub.fail_on.add("interaction_resolved")
    service = TalkToHarnessesService(
        p, AdapterRegistry(), fail_pub, _now, RuntimeManager(p, AdapterRegistry(), clock=_now)
    )
    state = await p.get_snapshot(cid, "owner")  # type: ignore[arg-type]
    turn_id = state.active_turn.id  # type: ignore[union-attr]
    interaction = _interaction(cid, turn_id)
    requested = request_interaction(state, interaction, now=_now())
    await p.commit_facade_mutation(
        cid,  # type: ignore[arg-type]
        "owner",
        state.conversation.version,
        requested.state,
        requested.events,
    )
    with pytest.raises(RuntimeError):
        await service.resolve_interaction(
            "owner",
            cid,  # type: ignore[arg-type]
            interaction.id,
            decision=ApprovalDecision.ALLOW_ONCE,
        )
    assert p.interaction_meta[interaction.id].get("released_at") is None

    # Recover with healthy publisher via reconcile.
    good = _Publisher()
    broker = InteractionBroker(p, good, clock=_now)
    await broker.reconcile_on_startup()
    assert p.interaction_meta[interaction.id].get("released_at") is not None
    assert any(e.type == "interaction_resolved" for e in good.events)
    commands = [c for c in p.commands.values() if c.kind.value == "answer_interaction"]
    assert len(commands) == 1


@pytest.mark.asyncio
async def test_duplicate_manual_resolve_returns_same_command() -> None:
    p = MemoryPersistence()
    publisher = _Publisher()
    service, p, cid = await _seed_running_turn(p)
    service = TalkToHarnessesService(
        p, AdapterRegistry(), publisher, _now, RuntimeManager(p, AdapterRegistry(), clock=_now)
    )
    state = await p.get_snapshot(cid, "owner")  # type: ignore[arg-type]
    turn_id = state.active_turn.id  # type: ignore[union-attr]
    interaction = _interaction(cid, turn_id)
    requested = request_interaction(state, interaction, now=_now())
    await p.commit_facade_mutation(
        cid,  # type: ignore[arg-type]
        "owner",
        state.conversation.version,
        requested.state,
        requested.events,
    )
    first = await service.resolve_interaction(
        "owner",
        cid,
        interaction.id,
        decision=ApprovalDecision.ALLOW_ONCE,  # type: ignore[arg-type]
    )
    second = await service.resolve_interaction(
        "owner",
        cid,
        interaction.id,
        decision=ApprovalDecision.DENY,  # type: ignore[arg-type]
    )
    assert first.id == second.id
    assert p.interaction_answers[interaction.id].decision is ApprovalDecision.ALLOW_ONCE


@pytest.mark.asyncio
async def test_create_and_allow_creates_rule_and_command() -> None:
    p = MemoryPersistence()
    publisher = _Publisher()
    service, p, cid = await _seed_running_turn(p)
    service = TalkToHarnessesService(
        p, AdapterRegistry(), publisher, _now, RuntimeManager(p, AdapterRegistry(), clock=_now)
    )
    state = await p.get_snapshot(cid, "owner")  # type: ignore[arg-type]
    turn_id = state.active_turn.id  # type: ignore[union-attr]
    interaction = _interaction(cid, turn_id)
    requested = request_interaction(state, interaction, now=_now())
    await p.commit_facade_mutation(
        cid,  # type: ignore[arg-type]
        "owner",
        state.conversation.version,
        requested.state,
        requested.events,
    )
    rule = ApprovalRule(
        principal_id="owner",
        decision=ApprovalRuleDecision.ALLOW,
        scope=PrincipalGlobalRuleScope(),
        matcher=ExactArgvMatcher(argv=("tool", "a")),
        created_at=_now(),
        updated_at=_now(),
    )
    cmd = await service.resolve_interaction(
        "owner",
        cid,  # type: ignore[arg-type]
        interaction.id,
        decision=ApprovalDecision.ALLOW_ONCE,
        create_rule=rule,
    )
    assert cmd.kind.value == "answer_interaction"
    assert rule.id in p.approval_rules
    audits = await service.list_interaction_audits("owner")
    assert len(audits.items) == 1
    assert audits.items[0].deciding_rule_id == rule.id


@pytest.mark.asyncio
async def test_concurrent_manual_resolves_single_winner() -> None:
    import asyncio

    p = MemoryPersistence()
    publisher = _Publisher()
    service, p, cid = await _seed_running_turn(p)
    service = TalkToHarnessesService(
        p, AdapterRegistry(), publisher, _now, RuntimeManager(p, AdapterRegistry(), clock=_now)
    )
    state = await p.get_snapshot(cid, "owner")  # type: ignore[arg-type]
    turn_id = state.active_turn.id  # type: ignore[union-attr]
    interaction = _interaction(cid, turn_id)
    requested = request_interaction(state, interaction, now=_now())
    await p.commit_facade_mutation(
        cid,  # type: ignore[arg-type]
        "owner",
        state.conversation.version,
        requested.state,
        requested.events,
    )

    async def _resolve(decision: ApprovalDecision):
        return await service.resolve_interaction(
            "owner",
            cid,  # type: ignore[arg-type]
            interaction.id,
            decision=decision,
        )

    results = await asyncio.gather(
        _resolve(ApprovalDecision.ALLOW_ONCE),
        _resolve(ApprovalDecision.DENY),
        return_exceptions=True,
    )
    commands = [r for r in results if not isinstance(r, BaseException)]
    assert len(commands) >= 1
    ids = {c.id for c in commands}
    assert len(ids) == 1
    assert p.interaction_answers[interaction.id].decision in {
        ApprovalDecision.ALLOW_ONCE,
        ApprovalDecision.DENY,
    }
    # Single resolution event for the winner.
    assert sum(1 for e in publisher.events if e.type == "interaction_resolved") == 1


@pytest.mark.asyncio
async def test_manual_only_request_not_auto_resolved() -> None:
    p = MemoryPersistence()
    publisher = _Publisher()
    await _seed_running_turn(p)
    broker = InteractionBroker(p, publisher, clock=_now)
    cid = next(iter(p.states))
    state = await p.get_worker_snapshot(cid)
    turn_id = state.active_turn.id  # type: ignore[union-attr]
    await p.create_approval_rule(
        ApprovalRule(
            principal_id="owner",
            decision=ApprovalRuleDecision.ALLOW,
            scope=PrincipalGlobalRuleScope(),
            matcher=ExactArgvMatcher(argv=("anything",)),
            created_at=_now(),
            updated_at=_now(),
        )
    )
    interaction = PendingInteraction(
        conversation_id=cid,
        turn_id=turn_id,
        kind=InteractionKind.APPROVAL,
        request=ApprovalRequestPayload(
            summary="display only",
            available_decisions=(ApprovalDecision.ALLOW_ONCE, ApprovalDecision.DENY),
        ),
        created_at=_now(),
    )
    await broker.accept_request(cid, interaction)
    assert not any(e.type == "interaction_resolved" for e in publisher.events)
    assert p.interaction_meta[interaction.id].get("policy_evaluated_at") is not None

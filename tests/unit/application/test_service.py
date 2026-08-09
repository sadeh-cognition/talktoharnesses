"""TalkToHarnessesService facade tests (no Django-Ninja)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from tests.runtime.memory_persistence import MemoryPersistence

from talktoharnesses.application.service import TalkToHarnessesService
from talktoharnesses.domain import (
    ApprovalDecision,
    ApprovalRule,
    ApprovalRuleDecision,
    ConversationRuleScope,
    DomainError,
    ErrorCode,
    ExactArgvMatcher,
    ExecutableRuleScope,
    HarnessCapabilities,
    HarnessConfiguration,
    HarnessInstanceRuleScope,
    HarnessKind,
    InteractionKind,
    TurnStatus,
    UserRuleScope,
)
from talktoharnesses.domain.events import ConversationEvent
from talktoharnesses.domain.models import ApprovalRequestPayload, PendingInteraction
from talktoharnesses.domain.transitions import request_interaction, start_turn, submit_turn
from talktoharnesses.providers.registry import AdapterRegistry
from talktoharnesses.runtime.manager import RuntimeManager


def _now() -> datetime:
    return datetime(2026, 8, 8, 15, 0, 0, tzinfo=UTC)


class _Publisher:
    def __init__(self) -> None:
        self.events: list[ConversationEvent] = []
        self.started = False
        self.stopped = False

    async def publish(self, events: Sequence[ConversationEvent]) -> None:
        self.events.extend(events)

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True


class _FailResolutionOncePublisher(_Publisher):
    def __init__(self) -> None:
        super().__init__()
        self._failed = False

    async def publish(self, events: Sequence[ConversationEvent]) -> None:
        if not self._failed and any(event.type == "interaction_resolved" for event in events):
            self._failed = True
            raise RuntimeError("publisher unavailable")
        await super().publish(events)


class _ProbeAdapter:
    kind = HarnessKind.GROK

    async def probe(self, config: HarnessConfiguration) -> HarnessCapabilities:
        return HarnessCapabilities(
            kind=HarnessKind.GROK,
            version="1.0.0",
            supports_steer=True,
            models=(),
            modes=(),
        )


def _service(
    persistence: MemoryPersistence | None = None,
) -> tuple[TalkToHarnessesService, MemoryPersistence, _Publisher]:
    p = persistence or MemoryPersistence()
    registry = AdapterRegistry()
    registry.register(HarnessKind.GROK, lambda: _ProbeAdapter())  # type: ignore[arg-type, return-value]
    publisher = _Publisher()
    runtime = RuntimeManager(p, registry, clock=_now)
    service = TalkToHarnessesService(p, registry, publisher, _now, runtime)
    return service, p, publisher


@pytest.mark.asyncio
async def test_start_stop_idempotent() -> None:
    service, _p, publisher = _service()
    await service.start("worker-1")
    await service.start("worker-1")
    assert publisher.started is True
    await service.stop()
    await service.stop()
    assert publisher.stopped is True


@pytest.mark.asyncio
async def test_harness_create_probe_and_owner_isolation() -> None:
    service, _p, _pub = _service()
    config = HarnessConfiguration(kind=HarnessKind.GROK, working_directory="/tmp/ws")
    h = await service.create_harness("owner-a", name="local", configuration=config)
    assert h.owner_id == "owner-a"
    probe = await service.probe_harness("owner-a", h.id)
    assert probe.capabilities.version == "1.0.0"
    caps = await service.get_harness_capabilities("owner-a", h.id)
    assert caps.capabilities.version == "1.0.0"
    with pytest.raises(DomainError) as exc:
        await service.get_harness("owner-b", h.id)
    assert exc.value.code is ErrorCode.NOT_FOUND


@pytest.mark.asyncio
async def test_conversation_create_metadata_and_soft_delete() -> None:
    service, _p, publisher = _service()
    config = HarnessConfiguration(kind=HarnessKind.GROK, working_directory="/tmp/ws")
    h = await service.create_harness("owner", name="h", configuration=config)
    snap = await service.create_conversation("owner", h.id, title="Hello")
    assert snap.detail.conversation.display_title == "Hello"
    assert snap.sequence == 0

    pinned = await service.pin_conversation("owner", snap.detail.conversation.id)
    assert pinned.detail.conversation.pinned_at is not None
    assert any(e.type == "conversation_metadata_changed" for e in publisher.events)

    await service.soft_delete_conversation("owner", snap.detail.conversation.id)
    with pytest.raises(DomainError) as exc:
        await service.get_conversation("owner", snap.detail.conversation.id)
    assert exc.value.code is ErrorCode.NOT_FOUND


@pytest.mark.asyncio
async def test_submit_turn_idempotency() -> None:
    service, _p, publisher = _service()
    config = HarnessConfiguration(kind=HarnessKind.GROK, working_directory="/tmp/ws")
    h = await service.create_harness("owner", name="h", configuration=config)
    snap = await service.create_conversation("owner", h.id)
    cid = snap.detail.conversation.id

    with pytest.raises(DomainError):
        await service.submit_turn("owner", cid, prompt="x", idempotency_key="")

    first = await service.submit_turn("owner", cid, prompt="hello", idempotency_key="k1")
    assert first.turn.status is TurnStatus.QUEUED
    n_events = len(publisher.events)

    again = await service.submit_turn("owner", cid, prompt="hello", idempotency_key="k1")
    assert again.command.id == first.command.id
    assert len(publisher.events) == n_events

    with pytest.raises(DomainError) as exc:
        await service.submit_turn("owner", cid, prompt="different", idempotency_key="k1")
    assert exc.value.code is ErrorCode.IDEMPOTENCY_CONFLICT


@pytest.mark.asyncio
async def test_interrupt_persists_command() -> None:
    service, p, _pub = _service()
    config = HarnessConfiguration(kind=HarnessKind.GROK, working_directory="/tmp/ws")
    h = await service.create_harness("owner", name="h", configuration=config)
    snap = await service.create_conversation("owner", h.id)
    cid = snap.detail.conversation.id
    # Force an active turn via domain transitions + persistence.
    state = await p.get_snapshot(cid, "owner")
    r = submit_turn(state, prompt="go", idempotency_key="s1", now=_now())
    r = start_turn(r.state, now=_now())
    await p.commit_facade_mutation(
        cid, "owner", state.conversation.version, r.state, r.events, commands=()
    )
    # Re-store commands from submit for claimability is optional for interrupt path.
    cmd = await service.interrupt("owner", cid, idempotency_key="int-1")
    assert cmd.kind.value == "interrupt"
    assert cmd.target_turn_id == r.state.active_turn.id  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_interaction_resolve_creates_answer_command() -> None:
    service, p, publisher = _service()
    config = HarnessConfiguration(kind=HarnessKind.GROK, working_directory="/tmp/ws")
    h = await service.create_harness("owner", name="h", configuration=config)
    snap = await service.create_conversation("owner", h.id)
    cid = snap.detail.conversation.id
    state = await p.get_snapshot(cid, "owner")
    r = submit_turn(state, prompt="x", idempotency_key="a", now=_now())
    r = start_turn(r.state, now=_now())
    turn_id = r.state.active_turn.id  # type: ignore[union-attr]
    interaction = PendingInteraction(
        conversation_id=cid,
        turn_id=turn_id,
        kind=InteractionKind.APPROVAL,
        request=ApprovalRequestPayload(summary="ok", available_decisions=tuple(ApprovalDecision)),
        created_at=_now(),
    )
    r = request_interaction(r.state, interaction, now=_now())
    await p.commit_facade_mutation(
        cid,
        "owner",
        state.conversation.version,
        r.state,
        r.events,
        commands=tuple(r.state.commands.values()),
    )

    cmd = await service.resolve_interaction(
        "owner",
        cid,
        interaction.id,
        decision=ApprovalDecision.ALLOW_ONCE,
    )
    assert cmd.kind.value == "answer_interaction"
    assert any(e.type == "interaction_resolved" for e in publisher.events)
    assert p.interaction_answers[interaction.id].decision is ApprovalDecision.ALLOW_ONCE


@pytest.mark.asyncio
async def test_resolution_retry_republishes_before_releasing_command() -> None:
    p = MemoryPersistence()
    registry = AdapterRegistry()
    registry.register(HarnessKind.GROK, lambda: _ProbeAdapter())  # type: ignore[arg-type, return-value]
    publisher = _FailResolutionOncePublisher()
    runtime = RuntimeManager(p, registry, clock=_now)
    service = TalkToHarnessesService(p, registry, publisher, _now, runtime)
    config = HarnessConfiguration(kind=HarnessKind.GROK, working_directory="/tmp/ws")
    harness = await service.create_harness("owner", name="h", configuration=config)
    snapshot = await service.create_conversation("owner", harness.id)
    conversation_id = snapshot.detail.conversation.id
    state = await p.get_snapshot(conversation_id, "owner")
    queued = submit_turn(state, prompt="x", idempotency_key="a", now=_now())
    running = start_turn(queued.state, now=_now())
    interaction = PendingInteraction(
        conversation_id=conversation_id,
        turn_id=running.state.active_turn.id,  # type: ignore[union-attr]
        kind=InteractionKind.APPROVAL,
        request=ApprovalRequestPayload(
            available_decisions=(ApprovalDecision.ALLOW_ONCE, ApprovalDecision.CANCEL)
        ),
        created_at=_now(),
    )
    requested = request_interaction(running.state, interaction, now=_now())
    await p.commit_facade_mutation(
        conversation_id,
        "owner",
        state.conversation.version,
        requested.state,
        (*queued.events, *running.events, *requested.events),
    )

    with pytest.raises(RuntimeError, match="publisher unavailable"):
        await service.resolve_interaction(
            "owner",
            conversation_id,
            interaction.id,
            decision=ApprovalDecision.ALLOW_ONCE,
        )
    assert p.interaction_meta[interaction.id].get("released_at") is None

    command = await service.resolve_interaction(
        "owner",
        conversation_id,
        interaction.id,
        decision=ApprovalDecision.ALLOW_ONCE,
    )

    assert command.kind.value == "answer_interaction"
    assert [event.type for event in publisher.events] == ["interaction_resolved"]


@pytest.mark.asyncio
async def test_interrupt_cancellations_are_audited_without_answer_commands() -> None:
    service, p, publisher = _service()
    config = HarnessConfiguration(kind=HarnessKind.GROK, working_directory="/tmp/ws")
    harness = await service.create_harness("owner", name="h", configuration=config)
    snapshot = await service.create_conversation("owner", harness.id)
    conversation_id = snapshot.detail.conversation.id
    state = await p.get_snapshot(conversation_id, "owner")
    queued = submit_turn(state, prompt="x", idempotency_key="a", now=_now())
    running = start_turn(queued.state, now=_now())
    first = PendingInteraction(
        conversation_id=conversation_id,
        turn_id=running.state.active_turn.id,  # type: ignore[union-attr]
        kind=InteractionKind.APPROVAL,
        request=ApprovalRequestPayload(available_decisions=(ApprovalDecision.CANCEL,)),
        created_at=_now(),
    )
    requested = request_interaction(running.state, first, now=_now())
    second = first.model_copy(update={"id": uuid4()})
    requested_again = request_interaction(requested.state, second, now=_now())
    await p.commit_facade_mutation(
        conversation_id,
        "owner",
        state.conversation.version,
        requested_again.state,
        (*queued.events, *running.events, *requested.events, *requested_again.events),
    )

    await service._broker.cancel_open_for_interrupt(conversation_id)  # pyright: ignore[reportPrivateUsage]

    assert len(p.interaction_audits) == 2
    assert all(
        answer.decision is ApprovalDecision.CANCEL for answer in p.interaction_answers.values()
    )
    assert all(command.kind.value != "answer_interaction" for command in p.commands.values())
    assert await p.list_unreleased_resolutions() == ()
    assert [event.type for event in publisher.events] == [
        "interaction_resolved",
        "interaction_resolved",
    ]


@pytest.mark.asyncio
async def test_list_search_and_history_pages() -> None:
    service, p, _pub = _service()
    config = HarnessConfiguration(kind=HarnessKind.GROK, working_directory="/tmp/ws")
    h = await service.create_harness("owner", name="h", configuration=config)
    snap = await service.create_conversation("owner", h.id, title="Searchable Title")
    cid = snap.detail.conversation.id
    # Seed search document via message index.
    from talktoharnesses.domain.enums import MessageRole
    from talktoharnesses.domain.models import Message

    msg = Message(
        turn_id=uuid4(),
        role=MessageRole.USER,
        text="needle-token",
        created_at=_now(),
    )
    p.messages[cid] = {msg.id: msg}
    p._refresh_search(p.states[cid])  # pyright: ignore[reportPrivateUsage]

    found = await service.search_conversations("owner", "needle-token")
    assert len(found.items) == 1
    listed = await service.list_conversations("owner")
    assert len(listed.items) == 1
    turns = await service.page_turns("owner", cid)
    assert turns.items == ()
    replay = await service.replay_events("owner", cid, after_sequence=0)
    assert replay == ()


@pytest.mark.asyncio
async def test_rule_facade_projection_matches_persistence_projection() -> None:
    from talktoharnesses.domain import PrincipalGlobalRuleScope

    service, _p, _pub = _service()
    rule = ApprovalRule(
        principal_id="owner",
        decision=ApprovalRuleDecision.ALLOW,
        scope=PrincipalGlobalRuleScope(),
        matcher=ExactArgvMatcher(argv=("echo",)),
        created_at=_now(),
        updated_at=_now(),
    )
    created = await service.create_approval_rule("owner", rule)
    listed = await service.list_approval_rules("owner")
    got = await service.get_approval_rule("owner", created.id)
    assert created.model_dump() == got.model_dump()
    assert listed.items[0].model_dump() == created.model_dump()
    # Facade returns shared wire models, not ORM.
    assert created.__class__.__name__ == "ApprovalRuleProjection"


@pytest.mark.asyncio
async def test_rule_scopes_are_owner_scoped_before_create_and_replace() -> None:
    from talktoharnesses.domain import PrincipalGlobalRuleScope

    service, _p, _pub = _service()
    config = HarnessConfiguration(kind=HarnessKind.GROK, working_directory="/tmp/ws")
    owned_harness = await service.create_harness("owner", name="owned", configuration=config)
    foreign_harness = await service.create_harness("foreign", name="foreign", configuration=config)
    foreign_conversation = await service.create_conversation("foreign", foreign_harness.id)
    base = ApprovalRule(
        principal_id="owner",
        decision=ApprovalRuleDecision.ALLOW,
        scope=PrincipalGlobalRuleScope(),
        matcher=ExactArgvMatcher(argv=("echo",)),
        created_at=_now(),
        updated_at=_now(),
    )
    await service.create_approval_rule("owner", base)

    with pytest.raises(DomainError):
        await service.create_approval_rule(
            "owner",
            base.model_copy(
                update={
                    "id": uuid4(),
                    "scope": ConversationRuleScope(
                        conversation_id=foreign_conversation.detail.conversation.id
                    ),
                }
            ),
        )
    with pytest.raises(DomainError):
        await service.replace_approval_rule(
            "owner",
            base.model_copy(
                update={"scope": HarnessInstanceRuleScope(harness_instance_id=foreign_harness.id)}
            ),
        )
    with pytest.raises(DomainError):
        await service.create_approval_rule(
            "owner",
            base.model_copy(update={"id": uuid4(), "scope": UserRuleScope(user_id="foreign")}),
        )

    await service.create_approval_rule(
        "owner",
        base.model_copy(
            update={
                "id": uuid4(),
                "scope": HarnessInstanceRuleScope(harness_instance_id=owned_harness.id),
            }
        ),
    )


@pytest.mark.asyncio
async def test_executable_rule_scope_is_strictly_resolved(tmp_path: Path) -> None:
    service, _p, _pub = _service()
    executable = tmp_path / "tool"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o700)
    link = tmp_path / "tool-link"
    link.symlink_to(executable)
    rule = ApprovalRule(
        principal_id="owner",
        decision=ApprovalRuleDecision.ALLOW,
        scope=ExecutableRuleScope(resolved_executable=str(link)),
        matcher=ExactArgvMatcher(argv=("tool",)),
        created_at=_now(),
        updated_at=_now(),
    )

    created = await service.create_approval_rule("owner", rule)

    assert isinstance(created.scope, ExecutableRuleScope)
    assert created.scope.resolved_executable == str(executable)


@pytest.mark.asyncio
async def test_cross_owner_uuid_is_not_found() -> None:
    service, _p, _pub = _service()
    config = HarnessConfiguration(kind=HarnessKind.GROK, working_directory="/tmp/ws")
    h = await service.create_harness("owner-a", name="h", configuration=config)
    snap = await service.create_conversation("owner-a", h.id)
    with pytest.raises(DomainError) as exc:
        await service.submit_turn(
            "owner-b",
            snap.detail.conversation.id,
            prompt="x",
            idempotency_key="k",
        )
    assert exc.value.code is ErrorCode.INVALID_STATE  # get_snapshot style


@pytest.mark.asyncio
async def test_queued_prompt_steer_readiness_and_history_pages() -> None:
    service, _p, _pub = _service()
    config = HarnessConfiguration(kind=HarnessKind.GROK, working_directory="/tmp/ws")
    h = await service.create_harness("owner", name="h", configuration=config)
    snap = await service.create_conversation("owner", h.id)
    cid = snap.detail.conversation.id

    assert service.started is False
    assert service.coordinator is not None
    assert service.publisher is not None
    ready_bits = service.readiness_snapshot()
    assert "probe_fresh" in ready_bits
    assert await service.is_ready() is False

    await service.submit_turn("owner", cid, prompt="queued-1", idempotency_key="q1")
    edited = await service.edit_queued_prompt("owner", cid, prompt="queued-2")
    assert edited.detail.conversation.id == cid
    cancelled = await service.cancel_queued_prompt("owner", cid)
    assert cancelled is not None

    with pytest.raises(DomainError):
        await service.steer("owner", cid, prompt="nudge", idempotency_key="   ")

    messages = await service.page_messages("owner", cid)
    tools = await service.page_tools("owner", cid)
    plans = await service.page_plans("owner", cid)
    activity = await service.page_activity("owner", cid)
    pending = await service.list_pending_interactions("owner", cid)
    assert len(messages.items) >= 1
    assert tools.items == ()
    assert plans.items == ()
    assert activity.items == ()
    assert pending.items == ()

    hw = await service.get_high_water_sequence("owner", cid)
    assert hw >= 0
    stream_hw = await service.get_stream_high_water_sequence("owner", cid)
    assert stream_hw >= 0
    stream_snap = await service.get_stream_snapshot("owner", cid)
    assert stream_snap.detail.conversation.id == cid
    replay = await service.replay_stream_events("owner", cid, after_sequence=0)
    assert isinstance(replay, (tuple, list))


@pytest.mark.asyncio
async def test_conversation_metadata_retention_and_probe_views() -> None:
    service, _p, _pub = _service()
    config = HarnessConfiguration(kind=HarnessKind.GROK, working_directory="/tmp/ws")
    h = await service.create_harness("owner", name="h", configuration=config)
    snap = await service.create_conversation("owner", h.id)
    cid = snap.detail.conversation.id

    await service.archive_conversation("owner", cid)
    await service.unarchive_conversation("owner", cid)
    await service.pin_conversation("owner", cid)
    await service.unpin_conversation("owner", cid)
    until = datetime(2026, 9, 1, tzinfo=UTC)
    await service.snooze_conversation("owner", cid, until=until)
    await service.unsnooze_conversation("owner", cid)
    await service.set_retention_exemption("owner", cid, exempt=True)

    policy = await service.get_retention_policy("owner")
    assert policy.months >= 1
    replaced = await service.replace_retention_policy("owner", 3)
    assert replaced.months == 3
    preview = await service.preview_retention("owner")
    assert preview.cutoff is not None

    probe = await service.probe_harness("owner", h.id)
    assert probe.capabilities.kind is HarnessKind.GROK
    models = await service.get_harness_models("owner", h.id)
    modes = await service.get_harness_modes("owner", h.id)
    assert models == probe.capabilities.models
    assert modes == probe.capabilities.modes

    class _BoomAdapter:
        kind = HarnessKind.GROK

        async def probe(self, config: HarnessConfiguration) -> HarnessCapabilities:
            del config
            raise RuntimeError("native probe crashed")

    service._registry.create = lambda kind: _BoomAdapter()  # type: ignore[method-assign, return-value]
    with pytest.raises(DomainError) as exc:
        await service.probe_harness("owner", h.id)
    assert exc.value.code is ErrorCode.PROVIDER_INCOMPATIBLE

    with pytest.raises(DomainError):
        await service.interrupt("owner", cid)


@pytest.mark.asyncio
async def test_start_failure_rolls_back_and_shutdown_timeouts() -> None:
    import asyncio
    import time

    service, _p, publisher = _service()

    async def fail_acquire(_worker_id: str) -> None:
        raise RuntimeError("lease unavailable")

    service._coordinator.acquire_and_heartbeat = fail_acquire  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await service.start("worker-fail")
    assert service.started is False
    assert publisher.stopped is True or publisher.started is False

    async def hang() -> None:
        await asyncio.sleep(10)

    async def boom() -> None:
        raise RuntimeError("stop failed")

    deadline = time.monotonic() + 0.05
    await TalkToHarnessesService._run_shutdown_step(hang(), deadline, "hang")  # pyright: ignore[reportPrivateUsage]
    await TalkToHarnessesService._run_shutdown_step(boom(), deadline + 1, "boom")  # pyright: ignore[reportPrivateUsage]

    async def cancellable() -> None:
        await asyncio.sleep(10)

    task = asyncio.create_task(cancellable())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await TalkToHarnessesService._run_shutdown_step(task, deadline + 1, "cancel")  # pyright: ignore[reportPrivateUsage]

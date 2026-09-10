"""TalkToHarnessesService facade tests (no Django-Ninja)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from tests.runtime.conftest import FakeAdapter
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
from talktoharnesses.domain.enums import CommandKind, CommandStatus
from talktoharnesses.domain.events import ConversationEvent
from talktoharnesses.domain.models import (
    ApprovalRequestPayload,
    Command,
    PendingInteraction,
    SwitchHarnessPayload,
)
from talktoharnesses.domain.transitions import (
    complete_turn,
    register_activity,
    request_interaction,
    start_turn,
    submit_turn,
)
from talktoharnesses.providers.registry import AdapterRegistry
from talktoharnesses.runtime.manager import RuntimeManager
from talktoharnesses.runtime.policy import RuntimePolicy


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
async def test_conversation_detail_reports_the_binding_harness_and_policy() -> None:
    """The detail carries what a client needs to resume without a harness record.

    A conversation outlives the harness it was opened on, so a client resuming
    one cannot read the harness for its identity or its approval policy. Both
    live on the binding, and the detail is where they are published.
    """
    service, _p, _publisher = _service()
    config = HarnessConfiguration(kind=HarnessKind.GROK, working_directory="/tmp/ws", yolo=True)
    h = await service.create_harness("owner", name="h", configuration=config)
    snap = await service.create_conversation("owner", h.id)
    conversation_id = snap.detail.conversation.id

    assert snap.detail.harness_id == h.id
    assert snap.detail.yolo is True

    await service.delete_harness("owner", h.id)

    resumed = await service.get_conversation("owner", conversation_id)
    assert resumed.detail.harness_id == h.id
    assert resumed.detail.yolo is True
    assert resumed.detail.harness_kind is HarnessKind.GROK


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


@pytest.mark.asyncio
async def test_probe_harness_releases_adapter_after_success_and_failure() -> None:
    service, _p, _pub = _service()
    config = HarnessConfiguration(kind=HarnessKind.GROK, working_directory="/tmp/ws")
    h = await service.create_harness("owner", name="h", configuration=config)
    released: list[str] = []

    class _ClosableProbe(_ProbeAdapter):
        async def aclose(self) -> None:
            released.append("ok")

    service._registry.create = lambda kind: _ClosableProbe()  # type: ignore[method-assign, return-value]
    await service.probe_harness("owner", h.id)
    assert released == ["ok"]

    class _ClosableBoom:
        kind = HarnessKind.GROK

        async def probe(self, config: HarnessConfiguration) -> HarnessCapabilities:
            del config
            raise DomainError(ErrorCode.SANDBOX_UNAVAILABLE, "sandbox down")

        async def aclose(self) -> None:
            released.append("fail")

    service._registry.create = lambda kind: _ClosableBoom()  # type: ignore[method-assign, return-value]
    with pytest.raises(DomainError):
        await service.probe_harness("owner", h.id)
    assert released == ["ok", "fail"]


# ---------------------------------------------------------------------------
# Runtime release: explicit close and delete-closes-runtime
# ---------------------------------------------------------------------------


async def _live_runtime_service(
    tmp_path: Path,
    persistence: MemoryPersistence | None = None,
) -> tuple[TalkToHarnessesService, MemoryPersistence, RuntimeManager, FakeAdapter, Any]:
    """A service whose OPENCODE conversation has a started SDK-managed runtime."""
    persistence = persistence if persistence is not None else MemoryPersistence()
    registry = AdapterRegistry()
    FakeAdapter.instances.clear()
    registry.register(HarnessKind.OPENCODE, FakeAdapter)
    runtime = RuntimeManager(
        persistence,
        registry,
        policy=RuntimePolicy(start_resume_timeout=2.0, graceful_close_timeout=0.3),
        clock=_now,
    )
    service = TalkToHarnessesService(persistence, registry, _Publisher(), _now, runtime)
    config = HarnessConfiguration(kind=HarnessKind.OPENCODE, working_directory=str(tmp_path))
    harness = await service.create_harness("owner", name="h", configuration=config)
    cid = (await service.create_conversation("owner", harness.id)).detail.conversation.id
    await runtime.start(conversation_id=cid, owner_id="owner", configuration=config)
    managed = runtime.get_runtime(cid)
    assert managed is not None
    return service, persistence, runtime, FakeAdapter.instances[-1], cid


@pytest.mark.asyncio
async def test_close_runtime_releases_idle_runtime_and_keeps_history(tmp_path: Path) -> None:
    service, persistence, runtime, adapter, cid = await _live_runtime_service(tmp_path)

    await service.close_runtime("owner", cid)

    assert runtime.get_runtime(cid) is None
    assert not runtime._runtimes  # pyright: ignore[reportPrivateUsage]
    assert adapter.closed is True
    assert "session_closed" in {event.type for event in persistence.events[cid]}
    # The conversation and its native session survive for a later resume.
    snapshot = await service.get_conversation("owner", cid)
    assert snapshot.detail.conversation.id == cid
    state = await persistence.get_snapshot(cid, "owner")
    assert state.binding is not None and state.binding.native_session_id
    # Closing again is a no-op.
    await service.close_runtime("owner", cid)


@pytest.mark.asyncio
async def test_close_runtime_refuses_while_turn_is_active(tmp_path: Path) -> None:
    service, persistence, runtime, _adapter, cid = await _live_runtime_service(tmp_path)
    state = await persistence.get_snapshot(cid, "owner")
    running = start_turn(
        submit_turn(state, prompt="go", idempotency_key="s1", now=_now()).state, now=_now()
    )
    await persistence.commit_facade_mutation(
        cid, "owner", state.conversation.version, running.state, running.events, commands=()
    )

    with pytest.raises(DomainError) as exc:
        await service.close_runtime("owner", cid)

    assert exc.value.code is ErrorCode.CONVERSATION_BUSY
    assert runtime.get_runtime(cid) is not None


@pytest.mark.asyncio
async def test_close_runtime_rejects_foreign_owner(tmp_path: Path) -> None:
    service, _persistence, runtime, _adapter, cid = await _live_runtime_service(tmp_path)
    with pytest.raises(DomainError):
        await service.close_runtime("intruder", cid)
    assert runtime.get_runtime(cid) is not None


@pytest.mark.asyncio
async def test_soft_delete_closes_live_runtime(tmp_path: Path) -> None:
    service, _persistence, runtime, adapter, cid = await _live_runtime_service(tmp_path)

    await service.soft_delete_conversation("owner", cid)

    assert runtime.get_runtime(cid) is None
    assert not runtime._runtimes  # pyright: ignore[reportPrivateUsage]
    assert adapter.closed is True
    with pytest.raises(DomainError) as exc:
        await service.get_conversation("owner", cid)
    assert exc.value.code is ErrorCode.NOT_FOUND


@pytest.mark.asyncio
async def test_soft_delete_refuses_while_switch_is_in_flight(tmp_path: Path) -> None:
    """Delete releases the runtime, so it must not race a switch that replaces it."""
    service, persistence, runtime, adapter, cid = await _live_runtime_service(tmp_path)
    state = await persistence.get_snapshot(cid, "owner")
    switch = Command(
        conversation_id=cid,
        kind=CommandKind.SWITCH_HARNESS,
        status=CommandStatus.DELIVERED,
        idempotency_key="sw-delete",
        payload=SwitchHarnessPayload(
            configuration=HarnessConfiguration(
                kind=HarnessKind.OPENCODE, working_directory=str(tmp_path)
            )
        ),
        created_at=_now(),
    )
    pending = state.model_copy(update={"commands": {**state.commands, switch.id: switch}})
    await persistence.commit_facade_mutation(
        cid, "owner", state.conversation.version, pending, (), commands=(switch,)
    )

    with pytest.raises(DomainError) as exc:
        await service.soft_delete_conversation("owner", cid)

    assert exc.value.code is ErrorCode.CONVERSATION_BUSY
    assert runtime.get_runtime(cid) is not None
    assert adapter.closed is False
    assert "session_closed" not in {event.type for event in persistence.events[cid]}
    snapshot = await service.get_conversation("owner", cid)
    assert snapshot.detail.conversation.deleted_at is None


@pytest.mark.asyncio
async def test_soft_delete_loses_to_turn_queued_after_validation(tmp_path: Path) -> None:
    """A prompt queued between the busy check and the close keeps its runtime."""

    class TurnQueuesAfterSnapshot(MemoryPersistence):
        armed = False

        async def get_snapshot(self, conversation_id: Any, owner_id: str) -> Any:
            stale = await super().get_snapshot(conversation_id, owner_id)
            if self.armed:
                self.armed = False
                queued = submit_turn(stale, prompt="go", idempotency_key="race", now=_now())
                assert queued.command is not None
                await self.commit_facade_mutation(
                    conversation_id,
                    owner_id,
                    stale.conversation.version,
                    queued.state,
                    queued.events,
                    commands=(queued.command,),
                )
            return stale

    store = TurnQueuesAfterSnapshot()
    service, persistence, runtime, adapter, cid = await _live_runtime_service(tmp_path, store)
    store.armed = True

    with pytest.raises(DomainError) as exc:
        await service.soft_delete_conversation("owner", cid)

    assert exc.value.code is ErrorCode.CONVERSATION_BUSY
    assert runtime.get_runtime(cid) is not None
    assert adapter.closed is False
    assert "session_closed" not in {event.type for event in persistence.events[cid]}
    assert persistence.states[cid].queued_turn is not None
    assert persistence.states[cid].conversation.deleted_at is None


@pytest.mark.asyncio
async def test_close_runtime_refuses_while_background_activity_runs(tmp_path: Path) -> None:
    """BACKGROUND_ACTIVE is busy: the harness is still working after the turn."""
    service, persistence, runtime, adapter, cid = await _live_runtime_service(tmp_path)
    state = await persistence.get_snapshot(cid, "owner")
    running = start_turn(
        submit_turn(state, prompt="go", idempotency_key="s1", now=_now()).state, now=_now()
    )
    assert running.state.active_turn is not None
    with_activity = register_activity(
        running.state, parent_turn_id=running.state.active_turn.id, now=_now(), title="bg"
    )
    finished = complete_turn(with_activity.state, now=_now())
    assert finished.state.active_turn is None
    assert finished.state.idle_reap_eligible is False
    await persistence.commit_facade_mutation(
        cid,
        "owner",
        state.conversation.version,
        finished.state,
        running.events + with_activity.events + finished.events,
        commands=(),
    )

    with pytest.raises(DomainError) as exc:
        await service.close_runtime("owner", cid)

    assert exc.value.code is ErrorCode.CONVERSATION_BUSY
    assert runtime.get_runtime(cid) is not None
    assert adapter.closed is False


@pytest.mark.asyncio
async def test_close_runtime_refuses_while_switch_is_in_flight(tmp_path: Path) -> None:
    """An accepted switch replaces the binding; the current runtime stays until it settles."""
    service, persistence, runtime, adapter, cid = await _live_runtime_service(tmp_path)
    state = await persistence.get_snapshot(cid, "owner")
    switch = Command(
        conversation_id=cid,
        kind=CommandKind.SWITCH_HARNESS,
        status=CommandStatus.ACCEPTED,
        idempotency_key="sw1",
        payload=SwitchHarnessPayload(
            configuration=HarnessConfiguration(
                kind=HarnessKind.OPENCODE, working_directory=str(tmp_path)
            )
        ),
        created_at=_now(),
    )
    pending = state.model_copy(update={"commands": {**state.commands, switch.id: switch}})
    await persistence.commit_facade_mutation(
        cid, "owner", state.conversation.version, pending, (), commands=(switch,)
    )

    with pytest.raises(DomainError) as exc:
        await service.close_runtime("owner", cid)

    assert exc.value.code is ErrorCode.CONVERSATION_BUSY
    assert runtime.get_runtime(cid) is not None
    assert adapter.closed is False


@pytest.mark.asyncio
async def test_close_runtime_on_non_owning_worker_refuses(tmp_path: Path) -> None:
    """Runtimes are per worker: a close landing elsewhere must not report success."""
    service_a, persistence, runtime_a, adapter, cid = await _live_runtime_service(tmp_path)
    service_a._worker_id = "worker-a"  # pyright: ignore[reportPrivateUsage]
    persistence.ownership[cid] = ("worker-a", 1, _now() + timedelta(minutes=5))
    registry = AdapterRegistry()
    registry.register(HarnessKind.OPENCODE, FakeAdapter)
    runtime_b = RuntimeManager(persistence, registry, clock=_now)
    service_b = TalkToHarnessesService(persistence, registry, _Publisher(), _now, runtime_b)
    service_b._worker_id = "worker-b"  # pyright: ignore[reportPrivateUsage]

    with pytest.raises(DomainError) as exc:
        await service_b.close_runtime("owner", cid)

    assert exc.value.code is ErrorCode.CONVERSATION_BUSY
    assert exc.value.details["reason"] == "runtime_owned_by_other_worker"
    assert runtime_a.get_runtime(cid) is not None
    assert adapter.closed is False
    assert "session_closed" not in {event.type for event in persistence.events[cid]}

    # The owning worker closes it; afterwards the other worker's close is a no-op.
    await service_a.close_runtime("owner", cid)
    assert runtime_a.get_runtime(cid) is None
    await service_b.close_runtime("owner", cid)
    await service_a.close_runtime("owner", cid)


@pytest.mark.asyncio
async def test_close_runtime_ignores_expired_lease_of_other_worker(tmp_path: Path) -> None:
    """A dead worker's stale lease holds no runtime; closing is an idempotent no-op."""
    persistence = MemoryPersistence()
    registry = AdapterRegistry()
    registry.register(HarnessKind.OPENCODE, FakeAdapter)
    runtime = RuntimeManager(persistence, registry, clock=_now)
    service = TalkToHarnessesService(persistence, registry, _Publisher(), _now, runtime)
    service._worker_id = "worker-b"  # pyright: ignore[reportPrivateUsage]
    config = HarnessConfiguration(kind=HarnessKind.OPENCODE, working_directory=str(tmp_path))
    harness = await service.create_harness("owner", name="h", configuration=config)
    cid = (await service.create_conversation("owner", harness.id)).detail.conversation.id
    persistence.ownership[cid] = ("worker-a", 1, _now() - timedelta(seconds=1))

    await service.close_runtime("owner", cid)

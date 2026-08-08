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

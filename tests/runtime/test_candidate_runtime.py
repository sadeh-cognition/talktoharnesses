"""Transient candidate runtimes for durable switching and rotation."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pytest
from tests.runtime.conftest import (
    FakeAdapter,
    MemoryPersistence,
    SeedReply,
    child_modes_path,
    conversation_id_of,
    make_state,
)

from talktoharnesses.domain import DomainError, ErrorCode, HarnessKind
from talktoharnesses.domain.enums import ProcessStatus
from talktoharnesses.providers import AdapterRegistry
from talktoharnesses.runtime import RuntimeManager, RuntimePolicy


class FakeSdkAdapter(FakeAdapter):
    """Candidate without a child process; exercises the SDK-managed path."""

    sdk_managed = True


def _registry(factory: Callable[[], FakeAdapter]) -> AdapterRegistry:
    reg = AdapterRegistry()
    reg.register(HarnessKind.OPENCODE, factory)  # type: ignore[arg-type]
    return reg


def _argv() -> tuple[str, ...]:
    return (str(child_modes_path()), "silence", "5")


@pytest.mark.asyncio
async def test_candidate_starts_without_touching_live_state(
    persistence: MemoryPersistence,
    registry: AdapterRegistry,
    short_policy: RuntimePolicy,
    owned_python: Path,
) -> None:
    mgr = RuntimeManager(persistence, registry, policy=short_policy)
    cid = conversation_id_of(persistence)
    state = persistence.states[cid]
    assert state.binding is not None
    binding_id = uuid4()

    candidate = await mgr.start_candidate(
        conversation_id=cid,
        owner_id="owner-1",
        binding_id=binding_id,
        configuration=state.binding.configuration,
        argv=_argv(),
    )

    assert candidate.session.binding_id == binding_id
    assert candidate.session.native_session_id
    assert candidate.process_record.status is ProcessStatus.RUNNING
    # The candidate is invisible to the conversation and wrote no durable rows.
    assert mgr.get_runtime(cid) is None
    assert mgr.get_candidate(binding_id) is candidate
    assert persistence.events[cid] == []
    assert persistence.processes == {}
    assert persistence.launch_history[cid] == []
    assert persistence.states[cid].binding is state.binding

    await mgr.close_candidate(binding_id)
    assert mgr.get_candidate(binding_id) is None
    await mgr.shutdown()


@pytest.mark.asyncio
async def test_empty_handoff_sends_no_synthetic_turn(
    persistence: MemoryPersistence,
    short_policy: RuntimePolicy,
    workdir: Path,
    now: datetime,
) -> None:
    adapters: list[FakeAdapter] = []

    def factory() -> FakeAdapter:
        adapter = FakeSdkAdapter()
        adapters.append(adapter)
        return adapter

    store = MemoryPersistence()
    state = make_state(now=now, workdir=workdir)
    store.seed(state)
    mgr = RuntimeManager(store, _registry(factory), policy=short_policy)
    binding_id = uuid4()

    candidate = await mgr.start_candidate(
        conversation_id=state.conversation.id,
        owner_id=state.conversation.owner_id,
        binding_id=binding_id,
        configuration=state.binding.configuration,  # type: ignore[union-attr]
    )
    await mgr.seed_candidate(candidate, "")

    assert adapters[0].submissions == []
    await mgr.close_candidate(binding_id)
    assert adapters[0].closed is True


@pytest.mark.asyncio
async def test_seed_drains_content_until_terminal(
    short_policy: RuntimePolicy,
    workdir: Path,
    now: datetime,
) -> None:
    adapters: list[FakeAdapter] = []

    def factory() -> FakeAdapter:
        adapter = FakeSdkAdapter()
        adapters.append(adapter)
        return adapter

    store = MemoryPersistence()
    state = make_state(now=now, workdir=workdir)
    store.seed(state)
    mgr = RuntimeManager(store, _registry(factory), policy=short_policy)
    binding_id = uuid4()

    candidate = await mgr.start_candidate(
        conversation_id=state.conversation.id,
        owner_id=state.conversation.owner_id,
        binding_id=binding_id,
        configuration=state.binding.configuration,  # type: ignore[union-attr]
    )
    await mgr.seed_candidate(candidate, "[user]: hello")

    submitted = adapters[0].submissions
    assert len(submitted) == 1
    assert submitted[0].prompt == "[user]: hello"
    # Seeding never publishes or persists the candidate's own events.
    assert store.events[state.conversation.id] == []


@pytest.mark.parametrize(
    ("seed_reply", "code"),
    [
        ("interaction", ErrorCode.PROTOCOL_ERROR),
        ("failed", ErrorCode.PROTOCOL_ERROR),
        ("foreign_turn", ErrorCode.PROTOCOL_ERROR),
        ("silent", ErrorCode.PROTOCOL_ERROR),
        ("hang", ErrorCode.RUNTIME_TIMEOUT),
    ],
)
@pytest.mark.asyncio
async def test_seed_rejects_unusable_candidate(
    seed_reply: SeedReply,
    code: ErrorCode,
    short_policy: RuntimePolicy,
    workdir: Path,
    now: datetime,
) -> None:
    store = MemoryPersistence()
    state = make_state(now=now, workdir=workdir)
    store.seed(state)
    mgr = RuntimeManager(
        store,
        _registry(lambda: FakeSdkAdapter(seed_reply=seed_reply)),
        policy=short_policy,
    )
    binding_id = uuid4()

    candidate = await mgr.start_candidate(
        conversation_id=state.conversation.id,
        owner_id=state.conversation.owner_id,
        binding_id=binding_id,
        configuration=state.binding.configuration,  # type: ignore[union-attr]
    )
    with pytest.raises(DomainError) as exc:
        await mgr.seed_candidate(candidate, "[user]: hello", timeout=0.2)
    assert exc.value.code is code

    await mgr.close_candidate(binding_id)
    assert store.events[state.conversation.id] == []


@pytest.mark.asyncio
async def test_promotion_replaces_the_live_runtime(
    persistence: MemoryPersistence,
    registry: AdapterRegistry,
    short_policy: RuntimePolicy,
    owned_python: Path,
) -> None:
    mgr = RuntimeManager(persistence, registry, policy=short_policy)
    cid = conversation_id_of(persistence)
    config = persistence.states[cid].binding.configuration  # type: ignore[union-attr]
    await mgr.start(
        conversation_id=cid,
        owner_id="owner-1",
        configuration=config,
        argv=_argv(),
    )
    previous = mgr.get_runtime(cid)
    assert previous is not None

    binding_id = uuid4()
    candidate = await mgr.start_candidate(
        conversation_id=cid,
        owner_id="owner-1",
        binding_id=binding_id,
        configuration=config,
        argv=_argv(),
    )
    promoted = await mgr.promote_candidate(cid, binding_id)

    assert promoted is candidate
    assert mgr.get_runtime(cid) is candidate
    assert mgr.get_candidate(binding_id) is None

    await mgr.close_replaced_runtime(previous)
    # Closing the replaced runtime leaves the promoted one live.
    assert mgr.get_runtime(cid) is candidate
    assert previous.closed is True
    assert "session_closed" not in {e.type for e in persistence.events[cid]}

    await mgr.shutdown()


@pytest.mark.asyncio
async def test_candidates_count_against_runtime_capacity(
    persistence: MemoryPersistence,
    registry: AdapterRegistry,
    short_policy: RuntimePolicy,
    owned_python: Path,
) -> None:
    policy = short_policy.model_copy(update={"max_runtimes": 1})
    mgr = RuntimeManager(persistence, registry, policy=policy)
    cid = conversation_id_of(persistence)
    config = persistence.states[cid].binding.configuration  # type: ignore[union-attr]
    await mgr.start(
        conversation_id=cid,
        owner_id="owner-1",
        configuration=config,
        argv=_argv(),
    )

    with pytest.raises(DomainError) as exc:
        await mgr.start_candidate(
            conversation_id=cid,
            owner_id="owner-1",
            binding_id=uuid4(),
            configuration=config,
            argv=_argv(),
        )
    assert exc.value.code is ErrorCode.CONVERSATION_BUSY

    await mgr.shutdown()


@pytest.mark.asyncio
async def test_stale_binding_closes_the_live_runtime(
    persistence: MemoryPersistence,
    registry: AdapterRegistry,
    short_policy: RuntimePolicy,
    owned_python: Path,
) -> None:
    mgr = RuntimeManager(persistence, registry, policy=short_policy)
    cid = conversation_id_of(persistence)
    state = persistence.states[cid]
    assert state.binding is not None
    await mgr.start(
        conversation_id=cid,
        owner_id="owner-1",
        configuration=state.binding.configuration,
        argv=_argv(),
    )

    current = await persistence.get_worker_snapshot(cid)
    assert await mgr.ensure_binding_current(cid, current) is not None

    rotated = current.model_copy(
        update={
            "binding": current.binding.model_copy(  # type: ignore[union-attr]
                update={"native_session_id": "replacement-native"}
            )
        }
    )
    assert await mgr.ensure_binding_current(cid, rotated) is None
    assert mgr.get_runtime(cid) is None

    await mgr.shutdown()

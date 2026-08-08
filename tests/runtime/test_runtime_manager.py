"""RuntimeManager lifecycle, concurrency, idle reap, and shutdown."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pytest
from tests.runtime.conftest import (
    FakeAdapter,
    MemoryPersistence,
    child_modes_path,
    conversation_id_of,
    make_state,
)

from talktoharnesses.domain import DomainError, ErrorCode, HarnessKind
from talktoharnesses.domain.enums import ActivityStatus
from talktoharnesses.domain.models import BackgroundActivity, HarnessConfiguration
from talktoharnesses.providers import AdapterRegistry
from talktoharnesses.providers.adapter import StartSessionRequest
from talktoharnesses.runtime import ProcessHandle, RuntimeManager, RuntimePolicy
from talktoharnesses.runtime.supervisor import ProcessSupervisor


def _argv(*modes: str) -> tuple[str, ...]:
    return (str(child_modes_path()), *modes)


@pytest.mark.asyncio
async def test_start_and_close(
    persistence: MemoryPersistence,
    registry: AdapterRegistry,
    short_policy: RuntimePolicy,
    owned_python: Path,
    workdir: Path,
) -> None:
    mgr = RuntimeManager(persistence, registry, policy=short_policy)
    cid = conversation_id_of(persistence)
    state = persistence.states[cid]
    assert state.binding is not None
    config = state.binding.configuration
    session = await mgr.start(
        conversation_id=cid,
        owner_id="owner-1",
        configuration=config,
        argv=_argv("exit_code", "0"),
        executable_path=str(owned_python),
    )
    assert session.native_session_id
    assert mgr.get_runtime(cid) is not None
    # Launch history recorded.
    assert persistence.launch_history[cid]
    await mgr.close(cid, reason="test")
    assert mgr.get_runtime(cid) is None
    events = persistence.events[cid]
    types = {e.type for e in events}
    assert "session_started" in types
    assert "session_closed" in types
    assert types & {"process_exited", "process_forced_termination"}


@pytest.mark.asyncio
async def test_concurrent_start_same_conversation(
    persistence: MemoryPersistence,
    registry: AdapterRegistry,
    short_policy: RuntimePolicy,
    owned_python: Path,
) -> None:
    mgr = RuntimeManager(persistence, registry, policy=short_policy)
    cid = conversation_id_of(persistence)
    config = persistence.states[cid].binding.configuration  # type: ignore[union-attr]

    async def start_one() -> object:
        return await mgr.start(
            conversation_id=cid,
            owner_id="owner-1",
            configuration=config,
            argv=_argv("silence", "2"),
            executable_path=str(owned_python),
        )

    results = await asyncio.gather(start_one(), start_one(), return_exceptions=True)
    successes = [r for r in results if not isinstance(r, Exception)]
    failures = [r for r in results if isinstance(r, DomainError)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert failures[0].code is ErrorCode.CONVERSATION_BUSY
    await mgr.shutdown()


@pytest.mark.asyncio
async def test_distinct_adapter_instances(
    short_policy: RuntimePolicy,
    owned_python: Path,
    workdir: Path,
    now: datetime,
) -> None:
    created: list[FakeAdapter] = []

    def factory() -> FakeAdapter:
        adapter = FakeAdapter()
        created.append(adapter)
        return adapter

    store = MemoryPersistence()
    s1 = make_state(now=now, workdir=workdir, executable=str(owned_python), owner_id="o1")
    s2 = make_state(now=now, workdir=workdir, executable=str(owned_python), owner_id="o2")
    store.seed(s1)
    store.seed(s2)
    reg = AdapterRegistry()
    reg.register(HarnessKind.OPENCODE, factory)
    mgr = RuntimeManager(store, reg, policy=short_policy)

    await mgr.start(
        conversation_id=s1.conversation.id,
        owner_id="o1",
        configuration=s1.binding.configuration,  # type: ignore[union-attr]
        argv=_argv("silence", "1"),
        executable_path=str(owned_python),
    )
    await mgr.start(
        conversation_id=s2.conversation.id,
        owner_id="o2",
        configuration=s2.binding.configuration,  # type: ignore[union-attr]
        argv=_argv("silence", "1"),
        executable_path=str(owned_python),
    )
    assert len(created) == 2
    assert created[0] is not created[1]
    assert created[0].instance_id != created[1].instance_id
    await mgr.shutdown()


@pytest.mark.asyncio
async def test_idle_reap_preserves_native_id(
    persistence: MemoryPersistence,
    registry: AdapterRegistry,
    owned_python: Path,
) -> None:
    policy = RuntimePolicy(
        idle_reap=0.2,
        start_resume_timeout=5,
        creation_timeout=5,
        graceful_close_timeout=1,
        interrupt_timeout=1,
        terminate_escalation=0.2,
        shutdown_budget=2,
        silence_warning=60,
    )
    mgr = RuntimeManager(persistence, registry, policy=policy)
    cid = conversation_id_of(persistence)
    config = persistence.states[cid].binding.configuration  # type: ignore[union-attr]
    session = await mgr.start(
        conversation_id=cid,
        owner_id="owner-1",
        configuration=config,
        argv=_argv("silence", "5"),
        executable_path=str(owned_python),
    )
    native = session.native_session_id
    assert await mgr.reap_if_eligible(cid)
    assert mgr.get_runtime(cid) is None
    state = persistence.states[cid]
    assert state.binding is not None
    assert state.binding.native_session_id == native
    assert persistence.launch_history[cid]
    types = {e.type for e in persistence.events[cid]}
    assert "session_reaped" in types


@pytest.mark.asyncio
async def test_background_activity_suppresses_reap(
    persistence: MemoryPersistence,
    registry: AdapterRegistry,
    short_policy: RuntimePolicy,
    owned_python: Path,
    now: datetime,
) -> None:
    mgr = RuntimeManager(persistence, registry, policy=short_policy)
    cid = conversation_id_of(persistence)
    config = persistence.states[cid].binding.configuration  # type: ignore[union-attr]
    await mgr.start(
        conversation_id=cid,
        owner_id="owner-1",
        configuration=config,
        argv=_argv("silence", "2"),
        executable_path=str(owned_python),
    )
    # Mark a running background activity on the aggregate.
    state = persistence.states[cid]
    activity = BackgroundActivity(
        conversation_id=cid,
        parent_turn_id=uuid4(),
        status=ActivityStatus.RUNNING,
        created_at=now,
    )
    # Force idle_reap_eligible false via activity bookkeeping.
    from talktoharnesses.domain.enums import ConversationStatus

    persistence.states[cid] = state.model_copy(
        update={
            "activities": {activity.id: activity},
            "idle_reap_eligible": False,
            "conversation": state.conversation.model_copy(
                update={"status": ConversationStatus.BACKGROUND_ACTIVE}
            ),
        }
    )
    assert not await mgr.reap_if_eligible(cid)
    assert mgr.get_runtime(cid) is not None
    await mgr.shutdown()


@pytest.mark.asyncio
async def test_interrupt_timeout_escalates(
    short_policy: RuntimePolicy,
    owned_python: Path,
    workdir: Path,
    now: datetime,
) -> None:
    FakeAdapter.instances.clear()
    store = MemoryPersistence()
    state = make_state(now=now, workdir=workdir, executable=str(owned_python))
    store.seed(state)
    reg = AdapterRegistry()
    reg.register(
        HarnessKind.OPENCODE,
        lambda: FakeAdapter(hang_interrupt=True),
    )
    mgr = RuntimeManager(store, reg, policy=short_policy)
    cid = state.conversation.id
    await mgr.start(
        conversation_id=cid,
        owner_id="owner-1",
        configuration=state.binding.configuration,  # type: ignore[union-attr]
        argv=_argv("ignore_interrupt"),
        executable_path=str(owned_python),
    )
    with pytest.raises(DomainError) as ei:
        await mgr.interrupt(cid)
    assert ei.value.code is ErrorCode.RUNTIME_TIMEOUT
    assert mgr.get_runtime(cid) is None
    types = {event.type for event in store.events[cid]}
    assert "process_forced_termination" in types
    process = next(iter(store.processes.values()))
    assert process.status.value == "terminated"


@pytest.mark.asyncio
async def test_optimistic_conflict_on_lifecycle(
    persistence: MemoryPersistence,
    registry: AdapterRegistry,
    short_policy: RuntimePolicy,
    owned_python: Path,
) -> None:
    mgr = RuntimeManager(persistence, registry, policy=short_policy)
    cid = conversation_id_of(persistence)
    # Corrupt version to force conflict on first process STARTING commit.
    state = persistence.states[cid]
    persistence.states[cid] = state.model_copy(
        update={"conversation": state.conversation.model_copy(update={"version": 99})}
    )
    # get_snapshot returns version 99; commit expects 99 but we'll desync mid-flight
    # by changing version after get — simpler: commit with wrong expected.
    with pytest.raises(DomainError) as ei:
        await persistence.commit_runtime_lifecycle(
            cid,
            0,  # wrong
            state,
            None,
            None,
            (),
        )
    assert ei.value.code is ErrorCode.OPTIMISTIC_CONFLICT
    await mgr.shutdown()


@pytest.mark.asyncio
async def test_shutdown_idempotent(
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
        argv=_argv("ignore_interrupt"),
        executable_path=str(owned_python),
    )
    await mgr.shutdown()
    await mgr.shutdown()  # idempotent
    assert mgr.get_runtime(cid) is None
    with pytest.raises(DomainError):
        await mgr.start(
            conversation_id=cid,
            owner_id="owner-1",
            configuration=config,
            argv=_argv("exit_code", "0"),
            executable_path=str(owned_python),
        )


@pytest.mark.asyncio
async def test_fresh_adapter_after_reap_resume(
    persistence: MemoryPersistence,
    owned_python: Path,
) -> None:
    created: list[FakeAdapter] = []

    def factory() -> FakeAdapter:
        adapter = FakeAdapter()
        created.append(adapter)
        return adapter

    reg = AdapterRegistry()
    reg.register(HarnessKind.OPENCODE, factory)
    policy = RuntimePolicy(
        idle_reap=60,
        start_resume_timeout=5,
        creation_timeout=5,
        graceful_close_timeout=1,
        interrupt_timeout=1,
        terminate_escalation=0.2,
        shutdown_budget=2,
        silence_warning=60,
    )
    mgr = RuntimeManager(persistence, reg, policy=policy)
    cid = conversation_id_of(persistence)
    config = persistence.states[cid].binding.configuration  # type: ignore[union-attr]
    session = await mgr.start(
        conversation_id=cid,
        owner_id="owner-1",
        configuration=config,
        argv=_argv("silence", "5"),
        executable_path=str(owned_python),
    )
    native = session.native_session_id
    assert native
    assert len(created) == 1
    first = created[0]
    assert await mgr.reap_if_eligible(cid)
    await mgr.resume(
        conversation_id=cid,
        owner_id="owner-1",
        configuration=config,
        native_session_id=native,
        argv=_argv("exit_code", "0"),
        executable_path=str(owned_python),
    )
    assert len(created) == 2
    assert created[1] is not first
    await mgr.shutdown()


@pytest.mark.asyncio
async def test_abnormal_exit_session_failed(
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
        argv=_argv("exit_code", "7"),
        executable_path=str(owned_python),
    )
    # Wait for process exit to be observed.
    for _ in range(50):
        types = {e.type for e in persistence.events[cid]}
        if "process_exited" in types or "session_failed" in types:
            break
        await asyncio.sleep(0.05)
    types = {e.type for e in persistence.events[cid]}
    assert "process_exited" in types or "session_failed" in types
    await mgr.shutdown()


@pytest.mark.asyncio
async def test_injected_supervisor_uses_manager_redaction_patterns(
    persistence: MemoryPersistence,
    registry: AdapterRegistry,
    short_policy: RuntimePolicy,
    owned_python: Path,
) -> None:
    mgr = RuntimeManager(
        persistence,
        registry,
        policy=short_policy,
        supervisor=ProcessSupervisor(short_policy),
        redaction_patterns=("SECRET",),
    )
    cid = conversation_id_of(persistence)
    config = persistence.states[cid].binding.configuration  # type: ignore[union-attr]
    await mgr.start(
        conversation_id=cid,
        owner_id="owner-1",
        configuration=config,
        argv=_argv("secret_stderr"),
        executable_path=str(owned_python),
    )
    for _ in range(50):
        if any(process.status.value != "running" for process in persistence.processes.values()):
            break
        await asyncio.sleep(0.02)
    process = next(iter(persistence.processes.values()))
    assert "SECRET" not in process.redacted_stderr_tail
    assert "[REDACTED]" in process.redacted_stderr_tail


@pytest.mark.asyncio
async def test_launch_snapshot_survives_adapter_start_timeout(
    short_policy: RuntimePolicy,
    owned_python: Path,
    workdir: Path,
    now: datetime,
) -> None:
    store = MemoryPersistence()
    state = make_state(now=now, workdir=workdir, executable=str(owned_python))
    store.seed(state)
    reg = AdapterRegistry()
    reg.register(HarnessKind.OPENCODE, lambda: FakeAdapter(hang_start=True))
    mgr = RuntimeManager(store, reg, policy=short_policy)
    with pytest.raises(DomainError) as exc_info:
        await mgr.start(
            conversation_id=state.conversation.id,
            owner_id="owner-1",
            configuration=state.binding.configuration,  # type: ignore[union-attr]
            argv=_argv("silence", "5"),
            executable_path=str(owned_python),
        )
    assert exc_info.value.code is ErrorCode.RUNTIME_TIMEOUT
    stored = store.states[state.conversation.id]
    assert stored.binding is not None
    assert stored.binding.launch_snapshot is not None
    assert store.launch_history[state.conversation.id]


@pytest.mark.asyncio
async def test_sdk_client_is_closed_when_start_fails(
    short_policy: RuntimePolicy,
    owned_python: Path,
    workdir: Path,
    now: datetime,
) -> None:
    class FailingSdkAdapter(FakeAdapter):
        sdk_managed = True

        async def start(self, request: StartSessionRequest):
            del request
            raise DomainError(ErrorCode.PROTOCOL_ERROR, "injected SDK startup failure")

    created: list[FailingSdkAdapter] = []

    def factory() -> FailingSdkAdapter:
        adapter = FailingSdkAdapter()
        created.append(adapter)
        return adapter

    store = MemoryPersistence()
    state = make_state(now=now, workdir=workdir, executable=str(owned_python))
    store.seed(state)
    registry = AdapterRegistry()
    registry.register(HarnessKind.OPENCODE, factory)
    manager = RuntimeManager(store, registry, policy=short_policy)
    with pytest.raises(DomainError) as exc_info:
        await manager.start(
            conversation_id=state.conversation.id,
            owner_id="owner-1",
            configuration=state.binding.configuration,  # type: ignore[union-attr]
            argv=(),
        )
    assert exc_info.value.code is ErrorCode.PROTOCOL_ERROR
    assert created[0].closed is True


@pytest.mark.asyncio
async def test_process_adapter_retries_one_pre_session_bind_failure(
    short_policy: RuntimePolicy,
    owned_python: Path,
    workdir: Path,
    now: datetime,
) -> None:
    class RetryAdapter(FakeAdapter):
        def __init__(self) -> None:
            super().__init__()
            self.process: ProcessHandle | None = None
            self.start_calls = 0
            self.retry_calls = 0

        def build_argv(self, config: HarnessConfiguration) -> tuple[str, ...]:
            del config
            return _argv("exit_code", "7")

        def bind_process(self, process: ProcessHandle) -> None:
            self.process = process

        async def start(self, request: StartSessionRequest):
            self.start_calls += 1
            if self.start_calls == 1:
                assert self.process is not None
                await self.process.wait()
                raise DomainError(ErrorCode.RUNTIME_TIMEOUT, "bind failed")
            return await super().start(request)

        async def retry_startup(self, error: DomainError) -> tuple[str, ...]:
            assert error.code is ErrorCode.RUNTIME_TIMEOUT
            self.retry_calls += 1
            return _argv("silence", "2")

    adapter = RetryAdapter()
    store = MemoryPersistence()
    state = make_state(now=now, workdir=workdir, executable=str(owned_python))
    store.seed(state)
    registry = AdapterRegistry()
    registry.register(HarnessKind.OPENCODE, lambda: adapter)
    manager = RuntimeManager(store, registry, policy=short_policy)
    await manager.start(
        conversation_id=state.conversation.id,
        owner_id="owner-1",
        configuration=state.binding.configuration,  # type: ignore[union-attr]
        argv=(),
        executable_path=str(owned_python),
    )
    assert adapter.start_calls == 2
    assert adapter.retry_calls == 1
    records = list(store.processes.values())
    assert len(records) == 2
    assert any(record.status.value == "failed" for record in records)
    await manager.shutdown()


@pytest.mark.asyncio
async def test_shutdown_cancels_overlapping_start(
    short_policy: RuntimePolicy,
    owned_python: Path,
    workdir: Path,
    now: datetime,
) -> None:
    store = MemoryPersistence()
    state = make_state(now=now, workdir=workdir, executable=str(owned_python))
    store.seed(state)
    reg = AdapterRegistry()
    reg.register(HarnessKind.OPENCODE, lambda: FakeAdapter(start_delay=1))
    mgr = RuntimeManager(store, reg, policy=short_policy)
    start_task = asyncio.create_task(
        mgr.start(
            conversation_id=state.conversation.id,
            owner_id="owner-1",
            configuration=state.binding.configuration,  # type: ignore[union-attr]
            argv=_argv("silence", "5"),
            executable_path=str(owned_python),
        )
    )
    for _ in range(50):
        if store.processes:
            break
        await asyncio.sleep(0.01)
    await mgr.shutdown()
    assert isinstance((await asyncio.gather(start_task, return_exceptions=True))[0], BaseException)
    assert mgr.get_runtime(state.conversation.id) is None
    assert all(process.status.value != "running" for process in store.processes.values())


@pytest.mark.asyncio
async def test_lifecycle_conflict_is_retried(
    registry: AdapterRegistry,
    short_policy: RuntimePolicy,
    owned_python: Path,
    workdir: Path,
    now: datetime,
) -> None:
    class ConflictOncePersistence(MemoryPersistence):
        conflict = True

        async def commit_runtime_lifecycle(self, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
            events = args[5]
            if self.conflict and any(event.type == "process_exited" for event in events):  # type: ignore[union-attr]
                self.conflict = False
                raise DomainError(ErrorCode.OPTIMISTIC_CONFLICT, "injected conflict")
            return await super().commit_runtime_lifecycle(*args, **kwargs)  # type: ignore[arg-type]

    store = ConflictOncePersistence()
    state = make_state(now=now, workdir=workdir, executable=str(owned_python))
    store.seed(state)
    mgr = RuntimeManager(store, registry, policy=short_policy)
    await mgr.start(
        conversation_id=state.conversation.id,
        owner_id="owner-1",
        configuration=state.binding.configuration,  # type: ignore[union-attr]
        argv=_argv("exit_code", "0"),
        executable_path=str(owned_python),
    )
    for _ in range(50):
        if any(event.type == "process_exited" for event in store.events[state.conversation.id]):
            break
        await asyncio.sleep(0.02)
    assert any(event.type == "process_exited" for event in store.events[state.conversation.id])


@pytest.mark.asyncio
async def test_shutdown_force_phase_is_concurrent_and_within_budget(
    owned_python: Path,
    workdir: Path,
    now: datetime,
) -> None:
    store = MemoryPersistence()
    states = [
        make_state(
            now=now,
            workdir=workdir,
            executable=str(owned_python),
            owner_id=f"owner-{index}",
        )
        for index in range(2)
    ]
    for state in states:
        store.seed(state)
    registry = AdapterRegistry()
    registry.register(HarnessKind.OPENCODE, lambda: FakeAdapter(hang_close=True))
    policy = RuntimePolicy(
        creation_timeout=1,
        start_resume_timeout=1,
        idle_reap=60,
        silence_warning=60,
        interrupt_timeout=0.1,
        graceful_close_timeout=0.4,
        terminate_escalation=0.15,
        shutdown_budget=0.5,
    )
    manager = RuntimeManager(store, registry, policy=policy)
    for index, state in enumerate(states):
        await manager.start(
            conversation_id=state.conversation.id,
            owner_id=f"owner-{index}",
            configuration=state.binding.configuration,  # type: ignore[union-attr]
            argv=_argv("ignore_interrupt"),
            executable_path=str(owned_python),
        )

    started = time.monotonic()
    await manager.shutdown()
    elapsed = time.monotonic() - started
    assert elapsed < 0.8
    assert all(manager.get_runtime(state.conversation.id) is None for state in states)

"""RuntimeManager lifecycle, concurrency, idle reap, and shutdown."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from tests.runtime.conftest import (
    FakeAdapter,
    MemoryPersistence,
    ResumingSdkAdapter,
    conversation_id_of,
    make_state,
)
from tth_types.process import (
    ProcessExitedEvent,
    ProcessForcedTerminationEvent,
    ProcessSilenceWarningEvent,
    ProcessStderrTruncatedEvent,
)
from tth_types.split_api import ProcessFrame, ProcessSnapshot

from talktoharnesses.domain import DomainError, ErrorCode, HarnessKind, append_events, submit_turn
from talktoharnesses.domain.enums import ActivityStatus, CommandKind, CommandStatus, ProcessStatus
from talktoharnesses.domain.events import ProviderWarningPayload
from talktoharnesses.domain.models import (
    BackgroundActivity,
    Command,
    HarnessCapabilities,
    HarnessConfiguration,
    SwitchHarnessPayload,
)
from talktoharnesses.domain.transitions import ConversationState, start_turn
from talktoharnesses.providers import AdapterRegistry
from talktoharnesses.providers.adapter import (
    HarnessSession,
    ResumeSessionRequest,
    StartSessionRequest,
)
from talktoharnesses.remote.handle import RemoteProcessHandle
from talktoharnesses.runtime import RuntimeManager, RuntimePolicy
from talktoharnesses.runtime.manager import (
    _await_start_resume,  # pyright: ignore[reportPrivateUsage]
)


@pytest.mark.asyncio
async def test_remote_start_uses_split_owned_timeout_budget() -> None:
    class Remote:
        remote = True

    expected = HarnessSession(
        conversation_id=uuid4(),
        binding_id=uuid4(),
        kind=HarnessKind.CLAUDE,
    )

    async def delayed() -> HarnessSession:
        await asyncio.sleep(0.02)
        return expected

    actual = await _await_start_resume(
        Remote(),  # type: ignore[arg-type]
        delayed(),
        timeout=0.001,
    )

    assert actual is expected


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
    s1 = make_state(now=now, workdir=workdir, owner_id="o1")
    s2 = make_state(now=now, workdir=workdir, owner_id="o2")
    store.seed(s1)
    store.seed(s2)
    reg = AdapterRegistry()
    reg.register(HarnessKind.OPENCODE, factory)
    mgr = RuntimeManager(store, reg, policy=short_policy)

    await mgr.start(
        conversation_id=s1.conversation.id,
        owner_id="o1",
        configuration=s1.binding.configuration,  # type: ignore[union-attr]
    )
    await mgr.start(
        conversation_id=s2.conversation.id,
        owner_id="o2",
        configuration=s2.binding.configuration,  # type: ignore[union-attr]
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
async def test_idle_reap_does_not_close_runtime_after_concurrent_prompt(
    persistence: MemoryPersistence,
    registry: AdapterRegistry,
    short_policy: RuntimePolicy,
    owned_python: Path,
    now: datetime,
) -> None:
    policy = short_policy.model_copy(update={"idle_reap": 60})
    mgr = RuntimeManager(persistence, registry, policy=policy)
    cid = conversation_id_of(persistence)
    config = persistence.states[cid].binding.configuration  # type: ignore[union-attr]
    await mgr.start(
        conversation_id=cid,
        owner_id="owner-1",
        configuration=config,
    )
    managed = mgr.get_runtime(cid)
    assert managed is not None

    original_get_snapshot = persistence.get_snapshot
    inject_prompt = True

    async def get_snapshot_with_concurrent_prompt(
        conversation_id: Any,
        owner_id: str,
    ) -> Any:
        nonlocal inject_prompt
        state = await original_get_snapshot(conversation_id, owner_id)
        if inject_prompt:
            inject_prompt = False
            queued = submit_turn(
                state,
                prompt="arrived during reap",
                idempotency_key="reap-race",
                now=now,
            )
            await persistence.commit_turn_batch(
                conversation_id,
                state.conversation.version,
                queued.state,
                queued.events,
                (queued.command,),  # type: ignore[arg-type]
            )
        return state

    persistence.get_snapshot = get_snapshot_with_concurrent_prompt  # type: ignore[method-assign]

    assert not await mgr.reap_if_eligible(cid)
    assert mgr.get_runtime(cid) is managed
    assert managed.adapter.closed is False  # type: ignore[attr-defined]
    assert "session_reaped" not in {event.type for event in persistence.events[cid]}
    await mgr.shutdown()


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
    state = make_state(now=now, workdir=workdir)
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
    )
    with pytest.raises(DomainError) as ei:
        await mgr.interrupt(cid)
    assert ei.value.code is ErrorCode.RUNTIME_TIMEOUT
    assert mgr.get_runtime(cid) is None
    types = {event.type for event in store.events[cid]}
    # No local supervised process: the timed-out runtime settles as an exit
    # (the split-side process is terminated over HTTP by the remote handle).
    assert "process_exited" in types
    process = next(iter(store.processes.values()))
    assert process.status.value == "exited"


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
    )
    await mgr.shutdown()
    await mgr.shutdown()  # idempotent
    assert mgr.get_runtime(cid) is None
    with pytest.raises(DomainError):
        await mgr.start(
            conversation_id=cid,
            owner_id="owner-1",
            configuration=config,
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
async def test_launch_snapshot_survives_adapter_start_timeout(
    short_policy: RuntimePolicy,
    owned_python: Path,
    workdir: Path,
    now: datetime,
) -> None:
    store = MemoryPersistence()
    state = make_state(now=now, workdir=workdir)
    store.seed(state)
    reg = AdapterRegistry()
    reg.register(HarnessKind.OPENCODE, lambda: FakeAdapter(hang_start=True))
    policy = short_policy.model_copy(update={"start_resume_timeout": 0.05})
    mgr = RuntimeManager(store, reg, policy=policy)
    with pytest.raises(DomainError) as exc_info:
        await mgr.start(
            conversation_id=state.conversation.id,
            owner_id="owner-1",
            configuration=state.binding.configuration,  # type: ignore[union-attr]
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
    state = make_state(now=now, workdir=workdir)
    store.seed(state)
    registry = AdapterRegistry()
    registry.register(HarnessKind.OPENCODE, factory)
    manager = RuntimeManager(store, registry, policy=short_policy)
    with pytest.raises(DomainError) as exc_info:
        await manager.start(
            conversation_id=state.conversation.id,
            owner_id="owner-1",
            configuration=state.binding.configuration,  # type: ignore[union-attr]
        )
    assert exc_info.value.code is ErrorCode.PROTOCOL_ERROR
    assert created[0].closed is True


@pytest.mark.asyncio
async def test_shutdown_cancels_overlapping_start(
    short_policy: RuntimePolicy,
    owned_python: Path,
    workdir: Path,
    now: datetime,
) -> None:
    store = MemoryPersistence()
    state = make_state(now=now, workdir=workdir)
    store.seed(state)
    reg = AdapterRegistry()
    reg.register(HarnessKind.OPENCODE, lambda: FakeAdapter(start_delay=1))
    mgr = RuntimeManager(store, reg, policy=short_policy)
    start_task = asyncio.create_task(
        mgr.start(
            conversation_id=state.conversation.id,
            owner_id="owner-1",
            configuration=state.binding.configuration,  # type: ignore[union-attr]
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
    state = make_state(now=now, workdir=workdir)
    store.seed(state)
    mgr = RuntimeManager(store, registry, policy=short_policy)
    await mgr.start(
        conversation_id=state.conversation.id,
        owner_id="owner-1",
        configuration=state.binding.configuration,  # type: ignore[union-attr]
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
        )

    started = time.monotonic()
    await manager.shutdown()
    elapsed = time.monotonic() - started
    assert elapsed < 0.8
    assert all(manager.get_runtime(state.conversation.id) is None for state in states)


@pytest.mark.asyncio
@pytest.mark.parametrize("remote", [False, True], ids=["sdk", "remote-process"])
async def test_shutdown_force_phase_settles_sessions_and_releases_resources(
    remote: bool, workdir: Path, now: datetime, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MemoryPersistence()
    states = [make_state(now=now, workdir=workdir) for _ in range(2)]
    arrived = 0
    both_arrived = asyncio.Event()

    async def arrive() -> None:
        nonlocal arrived
        arrived += 1
        if arrived == len(states):
            both_arrived.set()
        await both_arrived.wait()

    async def terminate(reason: str | None) -> None:
        if reason == "shutdown":
            await arrive()

    async def close(session: HarnessSession) -> None:
        await arrive()

    def factory() -> FakeAdapter:
        if remote:
            adapter = _RemoteFakeAdapter()
            adapter.process_handle = RemoteProcessHandle(pid=4242, terminate=terminate)
            return adapter
        adapter = FakeAdapter()
        monkeypatch.setattr(adapter, "close", close)
        return adapter

    registry = AdapterRegistry()
    registry.register(HarnessKind.OPENCODE, factory)
    manager = RuntimeManager(
        store,
        registry,
        policy=RuntimePolicy(idle_reap=60, shutdown_budget=2, graceful_close_timeout=0.5),
    )
    for state in states:
        store.seed(state)
        assert state.binding is not None
        await manager.start(
            conversation_id=state.conversation.id,
            owner_id="owner-1",
            configuration=state.binding.configuration,
        )
    managed = [manager.get_runtime(state.conversation.id) for state in states]
    background_tasks = [
        task for runtime in managed if runtime is not None for task in runtime.tasks
    ]
    if remote:
        for runtime in managed:
            assert runtime is not None and isinstance(runtime.process, RemoteProcessHandle)
            # The latest snapshot reports truncated stderr, but the corresponding
            # notification was lost when the transport closed.
            runtime.process.mark_stream_closed()
            runtime.process.on_frame(
                ProcessFrame(
                    event=ProcessStderrTruncatedEvent(
                        process_id=runtime.process_record.id, retained_bytes=4
                    ),
                    snapshot=ProcessSnapshot(
                        pid=4242,
                        stderr_truncated=True,
                        retained_stderr_bytes=4,
                        redacted_stderr_tail="tail",
                    ),
                )
            )

    # An external shutdown deadline leaves only the force reserve, no grace period.
    await asyncio.wait_for(
        manager.shutdown(deadline=asyncio.get_running_loop().time() + 0.75), timeout=1.5
    )

    assert both_arrived.is_set(), "all runtimes must be forced concurrently"
    for state, runtime in zip(states, managed, strict=True):
        assert runtime is not None and runtime.closed
        assert manager.get_runtime(state.conversation.id) is None
        assert runtime.adapter.interrupt_calls == 0  # type: ignore[attr-defined]
        types = [event.type for event in store.events[state.conversation.id]]
        assert types.count("session_closed") == 1
        terminal = "process_forced_termination" if remote else "process_exited"
        assert types.count(terminal) == 1
        process = store.processes[runtime.process_record.id]
        assert process.status.value == ("terminated" if remote else "exited")
        if remote:
            assert runtime.process is not None and runtime.process.forced_reason == "shutdown"
            assert types.count("process_stderr_truncated") == 1
            assert types.index("process_stderr_truncated") < types.index(terminal)
            assert process.redacted_stderr_tail == "tail"
    assert all(task.done() for task in background_tasks)
    assert not manager._idle_tasks  # pyright: ignore[reportPrivateUsage]
    event_counts = {cid: len(events) for cid, events in store.events.items()}
    await manager.shutdown()
    assert {cid: len(events) for cid, events in store.events.items()} == event_counts


@pytest.mark.asyncio
@pytest.mark.parametrize("close_fails", [False, True])
async def test_shutdown_closes_unpromoted_remote_candidate_without_changing_binding(
    close_fails: bool,
    short_policy: RuntimePolicy,
    workdir: Path,
    now: datetime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryPersistence()
    state = make_state(now=now, workdir=workdir)
    store.seed(state)
    assert state.binding is not None
    registry = AdapterRegistry()
    registry.register(HarnessKind.OPENCODE, _RemoteFakeAdapter)
    manager = RuntimeManager(store, registry, policy=short_policy)
    binding_id = uuid4()
    candidate = await manager.start_candidate(
        conversation_id=state.conversation.id,
        owner_id="owner-1",
        binding_id=binding_id,
        configuration=state.binding.configuration,
    )
    close = AsyncMock(side_effect=RuntimeError("split close failed") if close_fails else None)
    monkeypatch.setattr(candidate.adapter, "close", close)

    await asyncio.wait_for(manager.shutdown(), timeout=1.5)

    close.assert_awaited_once_with(candidate.session)
    assert manager.get_candidate(binding_id) is None
    assert candidate.closed
    assert candidate.process is not None
    assert candidate.process.forced is close_fails
    if close_fails:
        assert candidate.process.forced_reason == "candidate_rejected"
    assert await store.get_worker_snapshot(state.conversation.id) == state
    assert not store.events.get(state.conversation.id)
    assert not store.processes
    with pytest.raises(DomainError) as exc:
        await manager.start_candidate(
            conversation_id=state.conversation.id,
            owner_id="owner-1",
            binding_id=uuid4(),
            configuration=state.binding.configuration,
        )
    assert exc.value.code is ErrorCode.INVALID_STATE


class _NoResumeSdkAdapter(FakeAdapter):
    sdk_managed = True


class _ResumeRejectingAdapter(ResumingSdkAdapter):
    async def resume(self, request: ResumeSessionRequest) -> HarnessSession:
        del request
        raise DomainError(ErrorCode.PROVIDER_INCOMPATIBLE, "native resume rejected")


@pytest.mark.asyncio
async def test_resume_for_recovery_happy_path(
    short_policy: RuntimePolicy,
    workdir: Path,
    now: datetime,
) -> None:
    from datetime import UTC, timedelta

    from talktoharnesses.domain.enums import RecoveryReasonCode
    from talktoharnesses.domain.models import LaunchSnapshot

    store = MemoryPersistence()
    state = make_state(now=now, workdir=workdir)
    assert state.binding is not None
    binding = state.binding.model_copy(update={"native_session_id": "native-resume-1"})
    store.seed(state.model_copy(update={"binding": binding}))
    cid = state.conversation.id
    store.ownership[cid] = ("worker-a", 3, datetime.now(UTC) + timedelta(hours=1))

    registry = AdapterRegistry()
    registry.register(HarnessKind.OPENCODE, ResumingSdkAdapter)
    mgr = RuntimeManager(store, registry, policy=short_policy)
    previous = LaunchSnapshot(
        harness_version="test-1",
        working_directory=str(workdir),
        adapter_version="0",
        capabilities=HarnessCapabilities(
            kind=HarnessKind.OPENCODE,
            version="test-1",
            supports_resume=True,
        ),
    )

    managed, reason = await mgr.resume_for_recovery(
        cid,
        "owner-1",
        binding.configuration,
        "native-resume-1",
        worker_id="worker-a",
        fence=3,
        expected_binding_kind=HarnessKind.OPENCODE,
        previous_launch=previous,
    )
    assert managed.session.native_session_id == "native-resume-1"
    assert mgr.get_runtime(cid) is managed
    assert reason is RecoveryReasonCode.UNCHANGED_LAUNCH
    await mgr.close(cid, reason="test")


@pytest.mark.asyncio
async def test_resume_for_recovery_rejects_busy_and_kind_mismatch(
    short_policy: RuntimePolicy,
    workdir: Path,
    now: datetime,
) -> None:
    from datetime import UTC, timedelta

    store = MemoryPersistence()
    state = make_state(now=now, workdir=workdir)
    assert state.binding is not None
    binding = state.binding.model_copy(update={"native_session_id": "n1"})
    store.seed(state.model_copy(update={"binding": binding}))
    cid = state.conversation.id
    store.ownership[cid] = ("worker-a", 1, datetime.now(UTC) + timedelta(hours=1))
    registry = AdapterRegistry()
    registry.register(HarnessKind.OPENCODE, ResumingSdkAdapter)
    mgr = RuntimeManager(store, registry, policy=short_policy)

    await mgr.resume_for_recovery(
        cid,
        "owner-1",
        binding.configuration,
        "n1",
        worker_id="worker-a",
        fence=1,
        expected_binding_kind=HarnessKind.OPENCODE,
        previous_launch=None,
    )
    with pytest.raises(DomainError) as busy:
        await mgr.resume_for_recovery(
            cid,
            "owner-1",
            binding.configuration,
            "n1",
            worker_id="worker-a",
            fence=1,
            expected_binding_kind=HarnessKind.OPENCODE,
            previous_launch=None,
        )
    assert busy.value.code is ErrorCode.CONVERSATION_BUSY

    store2 = MemoryPersistence()
    state2 = make_state(now=now, workdir=workdir)
    assert state2.binding is not None
    binding2 = state2.binding.model_copy(update={"native_session_id": "n2"})
    store2.seed(state2.model_copy(update={"binding": binding2}))
    cid2 = state2.conversation.id
    store2.ownership[cid2] = ("worker-a", 1, datetime.now(UTC) + timedelta(hours=1))
    mgr2 = RuntimeManager(store2, registry, policy=short_policy)
    with pytest.raises(DomainError) as kind_exc:
        await mgr2.resume_for_recovery(
            cid2,
            "owner-1",
            binding2.configuration,
            "n2",
            worker_id="worker-a",
            fence=1,
            expected_binding_kind=HarnessKind.GROK,
            previous_launch=None,
        )
    assert kind_exc.value.code is ErrorCode.INVALID_STATE
    await mgr.shutdown()
    await mgr2.shutdown()


@pytest.mark.asyncio
async def test_resume_for_recovery_unsupported_and_rejected(
    short_policy: RuntimePolicy,
    workdir: Path,
    now: datetime,
) -> None:
    from datetime import UTC, timedelta

    from talktoharnesses.domain.enums import RecoveryReasonCode

    store = MemoryPersistence()
    state = make_state(now=now, workdir=workdir)
    assert state.binding is not None
    binding = state.binding.model_copy(update={"native_session_id": "n1"})
    store.seed(state.model_copy(update={"binding": binding}))
    cid = state.conversation.id
    store.ownership[cid] = ("worker-a", 1, datetime.now(UTC) + timedelta(hours=1))
    registry = AdapterRegistry()
    registry.register(HarnessKind.OPENCODE, _NoResumeSdkAdapter)
    mgr = RuntimeManager(store, registry, policy=short_policy)
    with pytest.raises(DomainError) as exc:
        await mgr.resume_for_recovery(
            cid,
            "owner-1",
            binding.configuration,
            "n1",
            worker_id="worker-a",
            fence=1,
            expected_binding_kind=HarnessKind.OPENCODE,
            previous_launch=None,
        )
    assert exc.value.code is ErrorCode.PROVIDER_INCOMPATIBLE
    assert exc.value.message == RecoveryReasonCode.RESUME_UNSUPPORTED.value
    await mgr.shutdown()

    store2 = MemoryPersistence()
    state2 = make_state(now=now, workdir=workdir)
    assert state2.binding is not None
    binding2 = state2.binding.model_copy(update={"native_session_id": "n2"})
    store2.seed(state2.model_copy(update={"binding": binding2}))
    cid2 = state2.conversation.id
    store2.ownership[cid2] = ("worker-a", 1, datetime.now(UTC) + timedelta(hours=1))
    reg2 = AdapterRegistry()
    reg2.register(HarnessKind.OPENCODE, _ResumeRejectingAdapter)
    mgr2 = RuntimeManager(store2, reg2, policy=short_policy)
    with pytest.raises(DomainError) as rejected:
        await mgr2.resume_for_recovery(
            cid2,
            "owner-1",
            binding2.configuration,
            "n2",
            worker_id="worker-a",
            fence=1,
            expected_binding_kind=HarnessKind.OPENCODE,
            previous_launch=None,
        )
    assert rejected.value.code is ErrorCode.PROVIDER_INCOMPATIBLE
    await mgr2.shutdown()


@pytest.mark.asyncio
async def test_recovery_handoff_fallback_success_and_failure(
    short_policy: RuntimePolicy,
    workdir: Path,
    now: datetime,
) -> None:
    from datetime import UTC, timedelta
    from unittest.mock import AsyncMock

    store = MemoryPersistence()
    state = make_state(now=now, workdir=workdir)
    assert state.binding is not None
    store.seed(state)
    cid = state.conversation.id
    binding_id = state.binding.id
    store.ownership[cid] = ("worker-a", 2, datetime.now(UTC) + timedelta(hours=1))
    registry = AdapterRegistry()
    registry.register(HarnessKind.OPENCODE, ResumingSdkAdapter)
    mgr = RuntimeManager(store, registry, policy=short_policy)

    candidate = await mgr.recovery_handoff_fallback(
        cid,
        "owner-1",
        binding_id,
        state.binding.configuration,
        "handoff text",
        worker_id="worker-a",
        fence=2,
    )
    assert candidate is not None
    assert mgr.get_candidate(binding_id) is candidate
    await mgr.close_candidate(binding_id)

    # Failure path: start_candidate raises → requires_session_recreation.
    mgr.start_candidate = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]
    failed = await mgr.recovery_handoff_fallback(
        cid,
        "owner-1",
        uuid4(),
        state.binding.configuration,
        "handoff text",
        worker_id="worker-a",
        fence=2,
    )
    assert failed is None
    binding = store.states[cid].binding
    assert binding is not None
    assert binding.requires_session_recreation is True
    await mgr.shutdown()


def test_map_resume_reason_branches() -> None:
    from talktoharnesses.domain.enums import RecoveryReasonCode
    from talktoharnesses.runtime.manager import (
        _map_resume_reason,  # pyright: ignore[reportPrivateUsage]
    )

    assert (
        _map_resume_reason(  # pyright: ignore[reportPrivateUsage]
            DomainError(
                ErrorCode.PROVIDER_INCOMPATIBLE,
                RecoveryReasonCode.RESUME_UNSUPPORTED.value,
            )
        )
        is RecoveryReasonCode.RESUME_UNSUPPORTED
    )
    assert (
        _map_resume_reason(DomainError(ErrorCode.PROVIDER_INCOMPATIBLE, "other"))  # pyright: ignore[reportPrivateUsage]
        is RecoveryReasonCode.PROVIDER_INCOMPATIBLE
    )
    assert (
        _map_resume_reason(DomainError(ErrorCode.RUNTIME_TIMEOUT, "timeout"))  # pyright: ignore[reportPrivateUsage]
        is RecoveryReasonCode.RESUME_REJECTED
    )
    assert (
        _map_resume_reason(DomainError(ErrorCode.INVALID_STATE, "x"))  # pyright: ignore[reportPrivateUsage]
        is RecoveryReasonCode.RESUME_REJECTED
    )


@pytest.mark.asyncio
async def test_persist_failure_retries_conflict_then_swallows(
    short_policy: RuntimePolicy,
    workdir: Path,
    now: datetime,
) -> None:
    from talktoharnesses.domain.models import ProcessRecord

    store = MemoryPersistence()
    state = make_state(now=now, workdir=workdir)
    store.seed(state)
    cid = state.conversation.id
    registry = AdapterRegistry()
    registry.register(HarnessKind.OPENCODE, FakeAdapter)
    mgr = RuntimeManager(store, registry, policy=short_policy)

    calls = {"n": 0}
    original = store.commit_runtime_lifecycle

    async def flaky(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise DomainError(ErrorCode.OPTIMISTIC_CONFLICT, "retry")
        if calls["n"] == 2:
            raise DomainError(ErrorCode.INVALID_STATE, "give up")
        return await original(*args, **kwargs)

    store.commit_runtime_lifecycle = flaky  # type: ignore[method-assign]
    record = ProcessRecord(
        conversation_id=cid,
        binding_id=state.binding.id,  # type: ignore[union-attr]
        status=ProcessStatus.STARTING,
    )
    await mgr._persist_failure(  # pyright: ignore[reportPrivateUsage]
        cid,
        "owner-1",
        record,
        None,
        ErrorCode.INVALID_STATE.value,
        "boom",
    )
    assert calls["n"] == 2

    async def boom(*_a: object, **_k: object) -> object:
        raise RuntimeError("db down")

    store.commit_runtime_lifecycle = boom  # type: ignore[method-assign]
    await mgr._persist_failure(  # pyright: ignore[reportPrivateUsage]
        cid,
        "owner-1",
        record,
        None,
        ErrorCode.INVALID_STATE.value,
        "boom",
    )
    await mgr.shutdown()


@pytest.mark.asyncio
async def test_recovery_handoff_recreation_flag_failure_is_swallowed(
    short_policy: RuntimePolicy,
    workdir: Path,
    now: datetime,
) -> None:
    store = MemoryPersistence()
    state = make_state(now=now, workdir=workdir)
    store.seed(state)
    cid = state.conversation.id
    binding_id = state.binding.id  # type: ignore[union-attr]
    registry = AdapterRegistry()
    registry.register(HarnessKind.OPENCODE, ResumingSdkAdapter)
    mgr = RuntimeManager(store, registry, policy=short_policy)
    mgr.start_candidate = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]

    async def fail_recreation(*_a: object, **_k: object) -> None:
        raise RuntimeError("cannot mark")

    store.commit_rotation_requires_recreation = fail_recreation  # type: ignore[method-assign]
    failed = await mgr.recovery_handoff_fallback(
        cid,
        "owner-1",
        binding_id,
        state.binding.configuration,  # type: ignore[union-attr]
        "handoff",
        worker_id="worker-a",
        fence=1,
    )
    assert failed is None
    await mgr.shutdown()


@pytest.mark.asyncio
async def test_resume_for_recovery_rejects_when_shutting_down(
    short_policy: RuntimePolicy,
    workdir: Path,
    now: datetime,
) -> None:
    from datetime import UTC, timedelta

    store = MemoryPersistence()
    state = make_state(now=now, workdir=workdir)
    assert state.binding is not None
    binding = state.binding.model_copy(update={"native_session_id": "n1"})
    store.seed(state.model_copy(update={"binding": binding}))
    cid = state.conversation.id
    store.ownership[cid] = ("worker-a", 1, datetime.now(UTC) + timedelta(hours=1))
    registry = AdapterRegistry()
    registry.register(HarnessKind.OPENCODE, ResumingSdkAdapter)
    mgr = RuntimeManager(store, registry, policy=short_policy)
    mgr._shutting_down = True  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(DomainError) as exc:
        await mgr.resume_for_recovery(
            cid,
            "owner-1",
            binding.configuration,
            "n1",
            worker_id="worker-a",
            fence=1,
            expected_binding_kind=HarnessKind.OPENCODE,
            previous_launch=None,
        )
    assert exc.value.code is ErrorCode.INVALID_STATE
    await mgr.shutdown()


@pytest.mark.asyncio
async def test_resume_for_recovery_probe_failure_maps_incompatible(
    short_policy: RuntimePolicy,
    workdir: Path,
    now: datetime,
) -> None:
    from datetime import UTC, timedelta

    from talktoharnesses.domain.enums import RecoveryReasonCode

    class _ProbeFailSdk(ResumingSdkAdapter):
        async def probe(self, config: HarnessConfiguration):
            del config
            raise DomainError(ErrorCode.PROVIDER_INCOMPATIBLE, "probe refused")

    store = MemoryPersistence()
    state = make_state(now=now, workdir=workdir)
    assert state.binding is not None
    binding = state.binding.model_copy(update={"native_session_id": "n1"})
    store.seed(state.model_copy(update={"binding": binding}))
    cid = state.conversation.id
    store.ownership[cid] = ("worker-a", 1, datetime.now(UTC) + timedelta(hours=1))
    registry = AdapterRegistry()
    registry.register(HarnessKind.OPENCODE, _ProbeFailSdk)
    mgr = RuntimeManager(store, registry, policy=short_policy)
    with pytest.raises(DomainError) as exc:
        await mgr.resume_for_recovery(
            cid,
            "owner-1",
            binding.configuration,
            "n1",
            worker_id="worker-a",
            fence=1,
            expected_binding_kind=HarnessKind.OPENCODE,
            previous_launch=None,
        )
    assert exc.value.code is ErrorCode.PROVIDER_INCOMPATIBLE
    assert exc.value.message == RecoveryReasonCode.PROVIDER_INCOMPATIBLE.value
    await mgr.shutdown()


class _RemoteFakeAdapter(FakeAdapter):
    """FakeAdapter that mirrors a split-supervised process like RemoteHarnessAdapter."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        from talktoharnesses.remote.handle import RemoteProcessHandle

        async def _terminate(_reason: str | None) -> None:
            return None

        self.process_handle = RemoteProcessHandle(pid=4242, terminate=_terminate)


@pytest.mark.asyncio
async def test_idle_timer_reap_removes_remote_runtime_from_live_map(
    persistence: MemoryPersistence,
    owned_python: Path,
) -> None:
    """The idle timer's reap must free the capacity slot of a runtime with a lifecycle pump.

    Regression: ``_teardown_runtime`` cancelled the idle task that was running the
    reap, so the cancellation fired at the next await and skipped the live-map pop.
    """
    registry = AdapterRegistry()
    registry.register(HarnessKind.OPENCODE, _RemoteFakeAdapter)
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
    await mgr.start(conversation_id=cid, owner_id="owner-1", configuration=config)
    managed = mgr.get_runtime(cid)
    assert managed is not None
    assert managed.process is not None

    deadline = time.monotonic() + 3.0
    while mgr._runtimes and time.monotonic() < deadline:  # pyright: ignore[reportPrivateUsage]
        await asyncio.sleep(0.05)

    assert "session_reaped" in {e.type for e in persistence.events[cid]}
    assert mgr.get_runtime(cid) is None
    assert not mgr._runtimes  # pyright: ignore[reportPrivateUsage]
    await mgr.shutdown()


@pytest.mark.asyncio
async def test_idle_reap_frees_runtime_of_deleted_conversation(
    persistence: MemoryPersistence,
    registry: AdapterRegistry,
    short_policy: RuntimePolicy,
    owned_python: Path,
) -> None:
    """A conversation deleted underneath its runtime must not pin a capacity slot."""
    mgr = RuntimeManager(
        persistence, registry, policy=short_policy.model_copy(update={"idle_reap": 60})
    )
    cid = conversation_id_of(persistence)
    config = persistence.states[cid].binding.configuration  # type: ignore[union-attr]
    await mgr.start(conversation_id=cid, owner_id="owner-1", configuration=config)
    state = persistence.states[cid]
    persistence.states[cid] = state.model_copy(
        update={
            "conversation": state.conversation.model_copy(
                update={"deleted_at": state.conversation.updated_at}
            )
        }
    )

    assert await mgr.reap_if_eligible(cid)

    assert mgr.get_runtime(cid) is None
    assert not mgr._runtimes  # pyright: ignore[reportPrivateUsage]
    await mgr.shutdown()


@pytest.mark.asyncio
async def test_close_idle_loses_to_turn_started_after_snapshot(
    registry: AdapterRegistry,
    short_policy: RuntimePolicy,
    owned_python: Path,
    workdir: Path,
    now: datetime,
) -> None:
    """The close is reserved under OCC: a turn committed after the idle check wins."""

    class TurnStartsAfterSnapshot(MemoryPersistence):
        armed = False

        async def get_snapshot(self, conversation_id: UUID, owner_id: str) -> ConversationState:
            stale = await super().get_snapshot(conversation_id, owner_id)
            if self.armed:
                self.armed = False
                # Another worker path commits RUNNING between the snapshot and the reservation.
                running = start_turn(
                    submit_turn(stale, prompt="go", idempotency_key="race", now=now).state,
                    now=now,
                )
                await self.commit_facade_mutation(
                    conversation_id,
                    owner_id,
                    stale.conversation.version,
                    running.state,
                    running.events,
                    commands=(),
                )
            return stale

    store = TurnStartsAfterSnapshot()
    state = make_state(now=now, workdir=workdir)
    store.seed(state)
    cid = state.conversation.id
    mgr = RuntimeManager(store, registry, policy=short_policy.model_copy(update={"idle_reap": 60}))
    await mgr.start(
        conversation_id=cid,
        owner_id="owner-1",
        configuration=state.binding.configuration,  # type: ignore[union-attr]
    )
    store.armed = True

    with pytest.raises(DomainError) as exc:
        await mgr.close_idle(cid, reason="client_close")

    assert exc.value.code is ErrorCode.CONVERSATION_BUSY
    assert mgr.get_runtime(cid) is not None
    assert "session_closed" not in {e.type for e in store.events[cid]}
    assert store.states[cid].active_turn is not None
    await mgr.shutdown()


@pytest.mark.asyncio
async def test_close_idle_and_reap_refuse_while_switch_in_flight(
    persistence: MemoryPersistence,
    registry: AdapterRegistry,
    short_policy: RuntimePolicy,
    owned_python: Path,
    now: datetime,
) -> None:
    mgr = RuntimeManager(
        persistence, registry, policy=short_policy.model_copy(update={"idle_reap": 60})
    )
    cid = conversation_id_of(persistence)
    state = persistence.states[cid]
    config = state.binding.configuration  # type: ignore[union-attr]
    await mgr.start(conversation_id=cid, owner_id="owner-1", configuration=config)
    switch = Command(
        conversation_id=cid,
        kind=CommandKind.SWITCH_HARNESS,
        status=CommandStatus.DELIVERY_STARTED,
        idempotency_key="sw",
        payload=SwitchHarnessPayload(configuration=config),
        created_at=now,
    )
    state = persistence.states[cid]
    persistence.states[cid] = state.model_copy(
        update={"commands": {**state.commands, switch.id: switch}}
    )

    assert await mgr.reap_if_eligible(cid) is False
    with pytest.raises(DomainError) as exc:
        await mgr.close_idle(cid, reason="client_close")
    assert exc.value.code is ErrorCode.CONVERSATION_BUSY
    assert mgr.get_runtime(cid) is not None

    settled = switch.model_copy(update={"status": CommandStatus.SETTLED})
    state = persistence.states[cid]
    persistence.states[cid] = state.model_copy(
        update={"commands": {**state.commands, switch.id: settled}}
    )
    assert await mgr.close_idle(cid, reason="client_close") is True
    assert mgr.get_runtime(cid) is None
    assert "session_closed" in {e.type for e in persistence.events[cid]}
    # Without a local runtime the close reports False instead of success.
    assert await mgr.close_idle(cid, reason="client_close") is False
    await mgr.shutdown()


class _DeletedAfterReservation(MemoryPersistence):
    """Another worker deletes the conversation once the session close is durable."""

    armed = False
    lifecycle_commits = 0

    async def commit_runtime_lifecycle(self, *args: Any, **kwargs: Any) -> Any:
        events = await super().commit_runtime_lifecycle(*args, **kwargs)
        self.lifecycle_commits += 1
        if self.armed:
            self.armed = False
            cid = args[0]
            state = self.states[cid]
            self.states[cid] = state.model_copy(
                update={
                    "conversation": state.conversation.model_copy(
                        update={"deleted_at": state.conversation.updated_at}
                    )
                }
            )
        return events


async def _started_manager(
    store: MemoryPersistence,
    registry: AdapterRegistry,
    short_policy: RuntimePolicy,
    workdir: Path,
    now: datetime,
) -> tuple[RuntimeManager, UUID]:
    state = make_state(now=now, workdir=workdir)
    store.seed(state)
    cid = state.conversation.id
    mgr = RuntimeManager(store, registry, policy=short_policy.model_copy(update={"idle_reap": 60}))
    await mgr.start(
        conversation_id=cid,
        owner_id="owner-1",
        configuration=state.binding.configuration,  # type: ignore[union-attr]
    )
    return mgr, cid


@pytest.mark.asyncio
@pytest.mark.parametrize("forced", [False, True], ids=["process-crash", "split-forced-kill"])
async def test_remote_process_failure_stream_rebases_events_and_releases_runtime(
    forced: bool,
    short_policy: RuntimePolicy,
    workdir: Path,
    now: datetime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryPersistence()
    registry = AdapterRegistry()
    registry.register(HarnessKind.OPENCODE, _RemoteFakeAdapter)
    manager, cid = await _started_manager(store, registry, short_policy, workdir, now)
    managed = manager.get_runtime(cid)
    assert managed is not None and managed.process is not None
    handle = managed.process
    assert isinstance(handle, RemoteProcessHandle)
    process_id = managed.process_record.id
    tasks = list(managed.tasks)
    original_commit = store.commit_runtime_lifecycle
    raced = False

    async def commit_with_concurrent_update(*args: Any, **kwargs: Any) -> Any:
        nonlocal raced
        if not raced:
            raced = True
            current = await store.get_worker_snapshot(cid)
            changed, events = append_events(
                current, now, [ProviderWarningPayload(message="concurrent update")]
            )
            await store.commit_turn_batch(cid, current.conversation.version, changed, events)
        return await original_commit(*args, **kwargs)

    monkeypatch.setattr(store, "commit_runtime_lifecycle", commit_with_concurrent_update)
    snapshot = ProcessSnapshot(
        pid=4242,
        returncode=-9 if forced else 17,
        forced=forced,
        forced_reason="split_shutdown" if forced else None,
        redacted_stderr_tail="provider failed: [REDACTED]",
        stderr_truncated=True,
        retained_stderr_bytes=27,
    )
    terminal = (
        ProcessForcedTerminationEvent(process_id=process_id, reason="split_shutdown")
        if forced
        else ProcessExitedEvent(process_id=process_id, exit_code=17)
    )
    for event in (
        ProcessSilenceWarningEvent(process_id=process_id),
        ProcessStderrTruncatedEvent(process_id=process_id, retained_bytes=27),
        terminal,
    ):
        handle.on_frame(ProcessFrame(event=event, snapshot=snapshot))
    handle.mark_stream_closed()
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=1)

    assert raced
    events = store.events[cid]
    assert (
        sum(
            isinstance(event.payload, ProviderWarningPayload)
            and event.payload.message == "concurrent update"
            for event in events
        )
        == 1
    )
    assert (
        sum(
            isinstance(event.payload, ProviderWarningPayload)
            and event.payload.code == "provider_silence"
            for event in events
        )
        == 1
    )
    types = [event.type for event in events]
    assert types.count("process_stderr_truncated") == 1
    terminal_type = "process_forced_termination" if forced else "process_exited"
    assert types.count(terminal_type) == 1
    assert types.index("process_stderr_truncated") < types.index(terminal_type)
    assert types.count("session_failed") == (0 if forced else 1)
    sequences = [event.sequence for event in events]
    assert sequences == list(range(sequences[0], sequences[0] + len(sequences)))
    process = store.processes[process_id]
    assert process.status.value == ("terminated" if forced else "failed")
    assert process.exit_code == snapshot.returncode
    assert process.redacted_stderr_tail == snapshot.redacted_stderr_tail
    assert manager.get_runtime(cid) is None
    assert managed.closed and not managed.tasks
    assert not manager._idle_tasks  # pyright: ignore[reportPrivateUsage]
    event_count = len(events)
    await manager.shutdown()
    assert len(store.events[cid]) == event_count


@pytest.mark.asyncio
async def test_close_idle_frees_slot_when_terminal_persist_fails(
    registry: AdapterRegistry,
    short_policy: RuntimePolicy,
    owned_python: Path,
    workdir: Path,
    now: datetime,
) -> None:
    """A reserved close whose process settlement fails must not pin the runtime."""
    store = _DeletedAfterReservation()
    mgr, cid = await _started_manager(store, registry, short_policy, workdir, now)
    store.armed = True

    with pytest.raises(DomainError) as exc:
        await mgr.close_idle(cid, reason="client_close")

    # The session close was reserved, then the terminal persist hit the delete.
    assert exc.value.code is ErrorCode.INVALID_STATE
    assert "session_closed" in {e.type for e in store.events[cid]}
    assert mgr.get_runtime(cid) is None
    assert not mgr._runtimes  # pyright: ignore[reportPrivateUsage]
    assert not mgr._idle_tasks  # pyright: ignore[reportPrivateUsage]
    await mgr.shutdown()


@pytest.mark.asyncio
async def test_close_frees_slot_when_terminal_persist_fails(
    registry: AdapterRegistry,
    short_policy: RuntimePolicy,
    owned_python: Path,
    workdir: Path,
    now: datetime,
) -> None:
    """The unreserved close path tears the runtime down on a persist failure too."""

    class SnapshotGone(MemoryPersistence):
        armed = False

        async def get_snapshot(self, conversation_id: UUID, owner_id: str) -> ConversationState:
            if self.armed:
                raise DomainError(ErrorCode.NOT_FOUND, "conversation not found")
            return await super().get_snapshot(conversation_id, owner_id)

    store = SnapshotGone()
    mgr, cid = await _started_manager(store, registry, short_policy, workdir, now)
    store.armed = True

    with pytest.raises(DomainError) as exc:
        await mgr.close(cid, reason="deleted")

    assert exc.value.code is ErrorCode.NOT_FOUND
    assert mgr.get_runtime(cid) is None
    assert not mgr._runtimes  # pyright: ignore[reportPrivateUsage]
    await mgr.shutdown()


@pytest.mark.asyncio
async def test_close_idle_forces_process_when_adapter_close_times_out(
    registry: AdapterRegistry,
    short_policy: RuntimePolicy,
    owned_python: Path,
    workdir: Path,
    now: datetime,
) -> None:
    """The reserved close escalates like the unreserved one instead of leaking the process."""
    store = MemoryPersistence()
    remote_registry = AdapterRegistry()
    remote_registry.register(HarnessKind.OPENCODE, _RemoteFakeAdapter)
    mgr, cid = await _started_manager(
        store,
        remote_registry,
        short_policy.model_copy(update={"graceful_close_timeout": 0.05}),
        workdir,
        now,
    )
    managed = mgr.get_runtime(cid)
    assert managed is not None and managed.process is not None

    async def _hang(_session: HarnessSession) -> None:
        await asyncio.sleep(10)

    managed.adapter.close = _hang  # type: ignore[method-assign]
    forced = AsyncMock()
    managed.process.force_terminate = forced  # type: ignore[method-assign]

    assert await mgr.close_idle(cid, reason="client_close") is True

    # The first escalation is the timeout; teardown may force again afterwards.
    forced.assert_awaited()
    assert forced.await_args_list[0].kwargs["reason"] == "graceful_close_timeout"
    assert mgr.get_runtime(cid) is None
    await mgr.shutdown()

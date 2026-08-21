"""RuntimeManager lifecycle, concurrency, idle reap, and shutdown."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from tests.runtime.conftest import (
    FakeAdapter,
    MemoryPersistence,
    child_modes_path,
    conversation_id_of,
    make_state,
)

from talktoharnesses.domain import DomainError, ErrorCode, HarnessKind, submit_turn
from talktoharnesses.domain.enums import ActivityStatus
from talktoharnesses.domain.models import (
    BackgroundActivity,
    HarnessCapabilities,
    HarnessConfiguration,
)
from talktoharnesses.providers import AdapterRegistry
from talktoharnesses.providers.adapter import (
    HarnessSession,
    ResumeSessionRequest,
    StartSessionRequest,
)
from talktoharnesses.runtime import ProcessHandle, RuntimeManager, RuntimePolicy
from talktoharnesses.runtime.supervisor import ProcessSupervisor


def _argv(*modes: str) -> tuple[str, ...]:
    return (str(child_modes_path()), *modes)


class _RejectingPreflightAdapter(FakeAdapter):
    def preflight_operation(self, mode: Literal["create", "resume"]) -> None:
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            f"{mode} matrix rejected",
        )


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
async def test_process_matrix_preflight_rejects_before_spawn(
    persistence: MemoryPersistence,
    short_policy: RuntimePolicy,
    owned_python: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = AdapterRegistry()
    registry.register(HarnessKind.OPENCODE, _RejectingPreflightAdapter)
    mgr = RuntimeManager(persistence, registry, policy=short_policy)
    spawn = AsyncMock()
    monkeypatch.setattr(mgr, "_spawn_process", spawn)
    cid = conversation_id_of(persistence)
    config = persistence.states[cid].binding.configuration  # type: ignore[union-attr]

    with pytest.raises(DomainError) as exc:
        await mgr.start(
            conversation_id=cid,
            owner_id="owner-1",
            configuration=config,
            argv=_argv("silence", "1"),
        )

    assert exc.value.code is ErrorCode.PROVIDER_INCOMPATIBLE
    spawn.assert_not_awaited()


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
        argv=_argv("silence", "1"),
    )
    await mgr.start(
        conversation_id=s2.conversation.id,
        owner_id="o2",
        configuration=s2.binding.configuration,  # type: ignore[union-attr]
        argv=_argv("silence", "1"),
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
        argv=_argv("silence", "5"),
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
        argv=_argv("silence", "2"),
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
        argv=_argv("ignore_interrupt"),
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
            argv=_argv("silence", "5"),
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
    state = make_state(now=now, workdir=workdir)
    store.seed(state)
    registry = AdapterRegistry()
    registry.register(HarnessKind.OPENCODE, lambda: adapter)
    manager = RuntimeManager(store, registry, policy=short_policy)
    await manager.start(
        conversation_id=state.conversation.id,
        owner_id="owner-1",
        configuration=state.binding.configuration,  # type: ignore[union-attr]
        argv=(),
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
            argv=_argv("silence", "5"),
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
        argv=_argv("exit_code", "0"),
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
            argv=_argv("ignore_interrupt"),
        )

    started = time.monotonic()
    await manager.shutdown()
    elapsed = time.monotonic() - started
    assert elapsed < 0.8
    assert all(manager.get_runtime(state.conversation.id) is None for state in states)


class _ResumingSdkAdapter(FakeAdapter):
    """SDK-managed adapter that advertises resume support."""

    sdk_managed = True

    async def probe(self, config: HarnessConfiguration):
        return HarnessCapabilities(
            kind=self.kind,
            version="test-1",
            supports_resume=True,
        )


class _NoResumeSdkAdapter(FakeAdapter):
    sdk_managed = True


class _ResumeRejectingAdapter(_ResumingSdkAdapter):
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
    registry.register(HarnessKind.OPENCODE, _ResumingSdkAdapter)
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
    registry.register(HarnessKind.OPENCODE, _ResumingSdkAdapter)
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
    registry.register(HarnessKind.OPENCODE, _ResumingSdkAdapter)
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
    from talktoharnesses.domain.enums import ProcessStatus
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
    registry.register(HarnessKind.OPENCODE, _ResumingSdkAdapter)
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
    registry.register(HarnessKind.OPENCODE, _ResumingSdkAdapter)
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

    class _ProbeFailSdk(_ResumingSdkAdapter):
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

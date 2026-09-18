"""Workspace setup (repo-declared .tth/setup.sh) around every session-opening path."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from tests.runtime.conftest import FakeAdapter, ResumingSdkAdapter, make_state
from tests.runtime.memory_persistence import MemoryPersistence

from talktoharnesses.domain import DomainError, ErrorCode, HarnessKind
from talktoharnesses.domain.enums import ProcessStatus
from talktoharnesses.domain.models import HarnessConfiguration
from talktoharnesses.domain.transitions import ConversationState
from talktoharnesses.providers import AdapterRegistry
from talktoharnesses.providers.adapter import (
    HarnessSession,
    ResumeSessionRequest,
    StartSessionRequest,
)
from talktoharnesses.remote.sandbox_workspace import (
    SETUP_FILE,
    WorkspaceSetupFailed,
    WorkspaceSetupFailureReason,
    WorkspaceSetupOutcome,
    WorkspaceSetupStarted,
)
from talktoharnesses.runtime import RuntimeManager, RuntimePolicy


def _exit_status_failure(workdir: Path) -> WorkspaceSetupFailed:
    return WorkspaceSetupFailed(
        "exit_status",
        kind=HarnessKind.OPENCODE,
        working_directory=str(workdir),
        message=f"{SETUP_FILE} exited with status 7",
        exit_code=7,
        output_tail="boom\n",
    )


class _ProvisioningAdapter(FakeAdapter):
    """Remote-shaped adapter exposing the ``prepare_workspace`` hook."""

    def __init__(self, *, outcome: Any = None, failure: WorkspaceSetupFailed | None = None) -> None:
        super().__init__()
        self.outcome = outcome
        self.failure = failure
        self.prepare_calls: list[HarnessConfiguration] = []
        self.start_calls = 0

    async def prepare_workspace(
        self, configuration: HarnessConfiguration, *, on_started: Any = None
    ) -> Any:
        self.prepare_calls.append(configuration)
        ran = self.failure is not None or (
            self.outcome is not None and self.outcome.status == "succeeded"
        )
        if on_started is not None and ran:
            on_started(
                WorkspaceSetupStarted(
                    working_directory=configuration.working_directory,
                    setup_file=".tth/setup.sh",
                    stamp="stamp-1",
                )
            )
            # Let the started event commit while "the script runs".
            await asyncio.sleep(0)
        if self.failure is not None:
            raise self.failure
        return self.outcome

    async def start(self, request: StartSessionRequest) -> HarnessSession:
        self.start_calls += 1
        return await super().start(request)


def _provisioning_manager(
    adapter: _ProvisioningAdapter,
    *,
    short_policy: RuntimePolicy,
    workdir: Path,
    now: datetime,
) -> tuple[RuntimeManager, MemoryPersistence, ConversationState]:
    store = MemoryPersistence()
    state = make_state(now=now, workdir=workdir)
    store.seed(state)
    reg = AdapterRegistry()
    reg.register(HarnessKind.OPENCODE, lambda: adapter)
    return RuntimeManager(store, reg, policy=short_policy), store, state


@pytest.mark.asyncio
async def test_workspace_setup_events_bracket_session_start(
    short_policy: RuntimePolicy, owned_python: Path, workdir: Path, now: datetime
) -> None:
    adapter = _ProvisioningAdapter(
        outcome=WorkspaceSetupOutcome(
            status="succeeded",
            working_directory=str(workdir),
            exit_code=0,
            duration_ms=42,
            output_tail="installed\n",
        )
    )
    mgr, store, state = _provisioning_manager(
        adapter, short_policy=short_policy, workdir=workdir, now=now
    )

    await mgr.start(
        conversation_id=state.conversation.id,
        owner_id="owner-1",
        configuration=state.binding.configuration,  # type: ignore[union-attr]
    )

    assert adapter.prepare_calls == [state.binding.configuration]  # type: ignore[union-attr]
    events = store.events[state.conversation.id]
    types = [event.type for event in events]
    assert types.index("workspace_setup_started") < types.index("workspace_setup_completed")
    assert types.index("workspace_setup_completed") < types.index("session_started")
    started = next(e.payload for e in events if e.type == "workspace_setup_started")
    completed = next(e.payload for e in events if e.type == "workspace_setup_completed")
    assert started.stamp == "stamp-1"  # type: ignore[union-attr]
    assert completed.status == "succeeded"  # type: ignore[union-attr]
    assert completed.duration_ms == 42  # type: ignore[union-attr]
    assert completed.output_tail == "installed\n"  # type: ignore[union-attr]
    assert [e.sequence for e in events] == list(range(1, len(events) + 1))
    await mgr.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", [None, "skipped", "absent"], ids=["disabled", "skip", "absent"])
async def test_quiet_workspace_outcomes_emit_no_events(
    short_policy: RuntimePolicy,
    owned_python: Path,
    workdir: Path,
    now: datetime,
    outcome: str | None,
) -> None:
    adapter = _ProvisioningAdapter(
        outcome=None
        if outcome is None
        else WorkspaceSetupOutcome(status=outcome, working_directory=str(workdir))  # type: ignore[arg-type]
    )
    mgr, store, state = _provisioning_manager(
        adapter, short_policy=short_policy, workdir=workdir, now=now
    )

    await mgr.start(
        conversation_id=state.conversation.id,
        owner_id="owner-1",
        configuration=state.binding.configuration,  # type: ignore[union-attr]
    )

    types = [event.type for event in store.events[state.conversation.id]]
    assert "workspace_setup_started" not in types
    assert "workspace_setup_completed" not in types
    assert "session_started" in types
    await mgr.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason", "status"), [("exit_status", "failed"), ("timeout", "timed_out")]
)
async def test_workspace_setup_failure_fails_the_session_before_the_harness_starts(
    short_policy: RuntimePolicy,
    owned_python: Path,
    workdir: Path,
    now: datetime,
    reason: WorkspaceSetupFailureReason,
    status: str,
) -> None:
    adapter = _ProvisioningAdapter(
        failure=WorkspaceSetupFailed(
            reason,
            kind=HarnessKind.OPENCODE,
            working_directory=str(workdir),
            message=f"{SETUP_FILE} exited with status 7",
            exit_code=7 if reason == "exit_status" else None,
            output_tail="npm ERR! boom\n",
        )
    )
    mgr, store, state = _provisioning_manager(
        adapter, short_policy=short_policy, workdir=workdir, now=now
    )

    with pytest.raises(DomainError) as exc_info:
        await mgr.start(
            conversation_id=state.conversation.id,
            owner_id="owner-1",
            configuration=state.binding.configuration,  # type: ignore[union-attr]
        )

    assert exc_info.value.code is ErrorCode.WORKSPACE_SETUP_FAILED
    assert adapter.start_calls == 0
    assert adapter.closed is True
    events = store.events[state.conversation.id]
    types = [event.type for event in events]
    assert types.index("workspace_setup_started") < types.index("workspace_setup_completed")
    assert types.index("workspace_setup_completed") < types.index("session_failed")
    assert "session_started" not in types
    completed = next(e.payload for e in events if e.type == "workspace_setup_completed")
    assert completed.status == status  # type: ignore[union-attr]
    assert completed.output_tail == "npm ERR! boom\n"  # type: ignore[union-attr]
    failed = next(e.payload for e in events if e.type == "session_failed")
    assert failed.error_code == "workspace_setup_failed"  # type: ignore[union-attr]
    assert SETUP_FILE in failed.message  # type: ignore[union-attr]
    assert mgr.get_runtime(state.conversation.id) is None


class _HangingProvisioningAdapter(_ProvisioningAdapter):
    """Setup that never finishes; models the uncancellable docker exec thread."""

    def __init__(self) -> None:
        super().__init__()
        self.captured_on_started: Any = None
        self.entered = asyncio.Event()

    async def prepare_workspace(
        self, configuration: HarnessConfiguration, *, on_started: Any = None
    ) -> Any:
        self.prepare_calls.append(configuration)
        self.captured_on_started = on_started
        self.entered.set()
        await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_setup_progress_after_cancelled_startup_cannot_revive_the_process(
    short_policy: RuntimePolicy, owned_python: Path, workdir: Path, now: datetime
) -> None:
    adapter = _HangingProvisioningAdapter()
    mgr, store, state = _provisioning_manager(
        adapter, short_policy=short_policy, workdir=workdir, now=now
    )
    cid = state.conversation.id
    task = asyncio.create_task(
        mgr.start(
            conversation_id=cid,
            owner_id="owner-1",
            configuration=state.binding.configuration,  # type: ignore[union-attr]
        )
    )
    await asyncio.wait_for(adapter.entered.wait(), timeout=1)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    types = [event.type for event in store.events[cid]]
    assert "session_failed" in types

    # The worker thread reports "start" only now, after the session failed.
    adapter.captured_on_started(
        WorkspaceSetupStarted(
            working_directory=str(workdir), setup_file=".tth/setup.sh", stamp="late"
        )
    )
    for _ in range(5):
        await asyncio.sleep(0)

    assert [event.type for event in store.events[cid]] == types
    processes = [record for record in store.processes.values() if record.conversation_id == cid]
    assert [record.status for record in processes] == [ProcessStatus.FAILED]
    assert mgr.get_runtime(cid) is None
    await mgr.shutdown()


class _ConflictOnceOnCompleted(MemoryPersistence):
    """A concurrent client write lands between the setup outcome and its commit."""

    def __init__(self) -> None:
        super().__init__()
        self.conflicts = 0

    async def commit_runtime_lifecycle(self, *args: Any, **kwargs: Any) -> Any:
        events = args[5]
        if self.conflicts == 0 and any(e.type == "workspace_setup_completed" for e in events):
            self.conflicts += 1
            raise DomainError(ErrorCode.OPTIMISTIC_CONFLICT, "optimistic concurrency conflict")
        return await super().commit_runtime_lifecycle(*args, **kwargs)


@pytest.mark.asyncio
async def test_setup_failure_outlives_a_conflicting_completion_commit(
    short_policy: RuntimePolicy, owned_python: Path, workdir: Path, now: datetime
) -> None:
    adapter = _ProvisioningAdapter(failure=_exit_status_failure(workdir))
    store = _ConflictOnceOnCompleted()
    state = make_state(now=now, workdir=workdir)
    store.seed(state)
    reg = AdapterRegistry()
    reg.register(HarnessKind.OPENCODE, lambda: adapter)
    mgr = RuntimeManager(store, reg, policy=short_policy)

    with pytest.raises(DomainError) as exc_info:
        await mgr.start(
            conversation_id=state.conversation.id,
            owner_id="owner-1",
            configuration=state.binding.configuration,  # type: ignore[union-attr]
        )

    # The conflict was retried; the setup outcome is what the client sees.
    assert exc_info.value.code is ErrorCode.WORKSPACE_SETUP_FAILED
    assert store.conflicts == 1
    events = store.events[state.conversation.id]
    types = [event.type for event in events]
    assert types.index("workspace_setup_completed") < types.index("session_failed")
    failed = next(e.payload for e in events if e.type == "session_failed")
    assert failed.error_code == "workspace_setup_failed"  # type: ignore[union-attr]
    await mgr.shutdown()


@pytest.mark.asyncio
async def test_candidate_runs_workspace_setup_without_events(
    short_policy: RuntimePolicy, owned_python: Path, workdir: Path, now: datetime
) -> None:
    adapter = _ProvisioningAdapter(
        outcome=WorkspaceSetupOutcome(
            status="succeeded", working_directory=str(workdir), exit_code=0, duration_ms=1
        )
    )
    mgr, store, state = _provisioning_manager(
        adapter, short_policy=short_policy, workdir=workdir, now=now
    )
    binding_id = uuid4()

    candidate = await mgr.start_candidate(
        conversation_id=state.conversation.id,
        owner_id="owner-1",
        binding_id=binding_id,
        configuration=state.binding.configuration,  # type: ignore[union-attr]
    )

    assert adapter.prepare_calls == [state.binding.configuration]  # type: ignore[union-attr]
    assert not store.events.get(state.conversation.id)
    assert mgr.get_candidate(binding_id) is candidate
    await mgr.shutdown()


class _ProvisioningResumeAdapter(_ProvisioningAdapter, ResumingSdkAdapter):
    """Resumable adapter with the ``prepare_workspace`` hook."""

    def __init__(self, *, outcome: Any = None, failure: WorkspaceSetupFailed | None = None) -> None:
        super().__init__(outcome=outcome, failure=failure)
        self.resume_calls = 0

    async def resume(self, request: ResumeSessionRequest) -> HarnessSession:
        self.resume_calls += 1
        return await super().resume(request)


def _recovery_fixture(
    adapter: _ProvisioningResumeAdapter,
    *,
    short_policy: RuntimePolicy,
    workdir: Path,
    now: datetime,
) -> tuple[RuntimeManager, MemoryPersistence, ConversationState]:
    store = MemoryPersistence()
    state = make_state(now=now, workdir=workdir)
    assert state.binding is not None
    binding = state.binding.model_copy(update={"native_session_id": "native-resume-1"})
    state = state.model_copy(update={"binding": binding})
    store.seed(state)
    store.ownership[state.conversation.id] = ("worker-a", 3, datetime.now(UTC) + timedelta(hours=1))
    registry = AdapterRegistry()
    registry.register(HarnessKind.OPENCODE, lambda: adapter)
    return RuntimeManager(store, registry, policy=short_policy), store, state


@pytest.mark.asyncio
async def test_recovery_resume_runs_workspace_setup_with_events(
    short_policy: RuntimePolicy, owned_python: Path, workdir: Path, now: datetime
) -> None:
    adapter = _ProvisioningResumeAdapter(
        outcome=WorkspaceSetupOutcome(
            status="succeeded", working_directory=str(workdir), exit_code=0, duration_ms=7
        )
    )
    mgr, store, state = _recovery_fixture(
        adapter, short_policy=short_policy, workdir=workdir, now=now
    )
    cid = state.conversation.id

    managed, _ = await mgr.resume_for_recovery(
        cid,
        "owner-1",
        state.binding.configuration,  # type: ignore[union-attr]
        "native-resume-1",
        worker_id="worker-a",
        fence=3,
        expected_binding_kind=HarnessKind.OPENCODE,
        previous_launch=None,
    )

    assert adapter.prepare_calls == [state.binding.configuration]  # type: ignore[union-attr]
    assert adapter.resume_calls == 1
    assert mgr.get_runtime(cid) is managed
    events = store.events[cid]
    types = [event.type for event in events]
    assert types.index("workspace_setup_started") < types.index("workspace_setup_completed")
    assert types.index("workspace_setup_completed") < types.index("session_resumed")
    assert [e.sequence for e in events] == list(range(1, len(events) + 1))
    await mgr.close(cid, reason="test")


@pytest.mark.asyncio
async def test_recovery_resume_fails_when_workspace_setup_fails(
    short_policy: RuntimePolicy, owned_python: Path, workdir: Path, now: datetime
) -> None:
    adapter = _ProvisioningResumeAdapter(failure=_exit_status_failure(workdir))
    mgr, store, state = _recovery_fixture(
        adapter, short_policy=short_policy, workdir=workdir, now=now
    )
    cid = state.conversation.id

    with pytest.raises(DomainError) as exc_info:
        await mgr.resume_for_recovery(
            cid,
            "owner-1",
            state.binding.configuration,  # type: ignore[union-attr]
            "native-resume-1",
            worker_id="worker-a",
            fence=3,
            expected_binding_kind=HarnessKind.OPENCODE,
            previous_launch=None,
        )

    assert exc_info.value.code is ErrorCode.WORKSPACE_SETUP_FAILED
    assert adapter.resume_calls == 0
    assert adapter.closed is True
    assert mgr.get_runtime(cid) is None
    types = [event.type for event in store.events[cid]]
    assert "workspace_setup_completed" in types
    assert "session_resumed" not in types
    await mgr.shutdown()

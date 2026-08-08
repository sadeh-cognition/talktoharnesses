"""ProcessSupervisor spawn, streams, timeouts, and tree termination."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from tests.runtime.conftest import child_modes_path, make_launch

from talktoharnesses.domain import DomainError, ErrorCode, HarnessCapabilities, HarnessKind
from talktoharnesses.runtime import (
    STDERR_RETENTION_BYTES,
    ProcessEvent,
    ProcessExitedEvent,
    ProcessHandle,
    ProcessSilenceWarningEvent,
    ProcessSpec,
    ProcessStartedEvent,
    ProcessStderrTruncatedEvent,
    ProcessSupervisor,
    RuntimePolicy,
)


def _spec(
    owned_python: Path,
    workdir: Path,
    *argv: str,
    process_id: UUID | None = None,
) -> ProcessSpec:
    launch = make_launch(executable=owned_python, workdir=workdir)
    return ProcessSpec(
        conversation_id=uuid4(),
        binding_id=uuid4(),
        process_id=process_id or uuid4(),
        launch=launch,
        argv=(str(child_modes_path()), *argv),
    )


@pytest.mark.asyncio
async def test_argv_preservation_and_stdout_bytes(owned_python: Path, workdir: Path) -> None:
    sup = ProcessSupervisor(RuntimePolicy(creation_timeout=5))
    payload = bytes([0x00, 0xFF, 0xFE, 0x01])
    handle = await sup.spawn(_spec(owned_python, workdir, "stdout_bytes", payload.hex()))
    chunks: list[bytes] = []
    async for c in handle.stdout():
        chunks.append(c)
    assert b"".join(chunks) == payload
    code = await handle.wait()
    assert code == 0
    await handle.close()


@pytest.mark.asyncio
async def test_malformed_stdout_never_in_stderr(owned_python: Path, workdir: Path) -> None:
    sup = ProcessSupervisor()
    handle = await sup.spawn(_spec(owned_python, workdir, "malformed_stdout"))
    out = b"".join([c async for c in handle.stdout()])
    assert b"\xff\xfe" in out
    assert b"stderr-only-marker" not in out
    await handle.wait()
    assert "stderr-only-marker" in handle.redacted_stderr_tail
    assert "\xff\xfe" not in handle.redacted_stderr_tail
    await handle.close()


@pytest.mark.asyncio
async def test_secret_split_redacted(owned_python: Path, workdir: Path) -> None:
    sup = ProcessSupervisor(redaction_patterns=("SECRET",))
    handle = await sup.spawn(_spec(owned_python, workdir, "secret_stderr"))
    await handle.wait()
    tail = handle.redacted_stderr_tail
    assert "SECRET" not in tail
    assert "[REDACTED]" in tail
    await handle.close()


@pytest.mark.asyncio
async def test_stderr_truncation_once(owned_python: Path, workdir: Path) -> None:
    sup = ProcessSupervisor(
        RuntimePolicy(creation_timeout=30),
    )
    # Write more than 10 MiB of stderr.
    n = STDERR_RETENTION_BYTES + 100_000
    handle = await sup.spawn(_spec(owned_python, workdir, "large_stderr", str(n)))
    events: list[ProcessEvent] = []

    async def collect() -> None:
        async for e in handle.events():
            events.append(e)

    collector = asyncio.create_task(collect())
    await handle.wait()
    await asyncio.sleep(0.1)
    await handle.close()
    collector.cancel()
    trunc = [e for e in events if isinstance(e, ProcessStderrTruncatedEvent)]
    assert len(trunc) == 1
    assert trunc[0].retained_bytes <= STDERR_RETENTION_BYTES
    assert len(handle.redacted_stderr_tail.encode("utf-8")) <= STDERR_RETENTION_BYTES


@pytest.mark.asyncio
async def test_silence_warning_and_reset(owned_python: Path, workdir: Path) -> None:
    policy = RuntimePolicy(silence_warning=0.15, creation_timeout=5)
    sup = ProcessSupervisor(policy)
    handle = await sup.spawn(_spec(owned_python, workdir, "silence", "0.4"))
    events: list[ProcessEvent] = []

    async def collect() -> None:
        async for e in handle.events():
            events.append(e)

    collector = asyncio.create_task(collect())
    # Consume stdout so silence timer can reset on late output.
    async for _ in handle.stdout():
        pass
    await handle.wait()
    await asyncio.sleep(0.05)
    await handle.close()
    collector.cancel()
    silence = [e for e in events if isinstance(e, ProcessSilenceWarningEvent)]
    assert len(silence) >= 1
    started = [e for e in events if isinstance(e, ProcessStartedEvent)]
    assert len(started) == 1


@pytest.mark.asyncio
async def test_abnormal_exit_code(owned_python: Path, workdir: Path) -> None:
    sup = ProcessSupervisor()
    handle = await sup.spawn(_spec(owned_python, workdir, "exit_code", "42"))
    events: list[ProcessEvent] = []

    async def collect() -> None:
        async for e in handle.events():
            events.append(e)

    collector = asyncio.create_task(collect())
    code = await handle.wait()
    assert code == 42
    await asyncio.sleep(0.05)
    await handle.close()
    collector.cancel()
    exited = [e for e in events if isinstance(e, ProcessExitedEvent)]
    assert exited and exited[0].exit_code == 42


@pytest.mark.asyncio
async def test_natural_exit_finishes_readers_and_event_stream(
    owned_python: Path,
    workdir: Path,
) -> None:
    sup = ProcessSupervisor(redaction_patterns=("SECRET",))
    handle = await sup.spawn(_spec(owned_python, workdir, "secret_stderr"))
    events = await asyncio.wait_for(
        asyncio.create_task(_collect_events(handle)),
        timeout=2,
    )
    assert isinstance(events[-1], ProcessExitedEvent)
    assert "SECRET" not in handle.redacted_stderr_tail
    assert "[REDACTED]" in handle.redacted_stderr_tail


async def _collect_events(handle: ProcessHandle) -> list[ProcessEvent]:
    return [event async for event in handle.events()]


@pytest.mark.asyncio
async def test_stdout_is_not_buffered_in_an_unbounded_queue(
    owned_python: Path,
    workdir: Path,
) -> None:
    handle = await ProcessSupervisor().spawn(_spec(owned_python, workdir, "stdout_bytes", "00ff"))
    assert not hasattr(handle, "_stdout_q")
    assert b"".join([chunk async for chunk in handle.stdout()]) == b"\x00\xff"
    await handle.wait()


@pytest.mark.asyncio
async def test_force_terminate_ignore_interrupt(owned_python: Path, workdir: Path) -> None:
    policy = RuntimePolicy(terminate_escalation=0.2, graceful_close_timeout=0.2)
    sup = ProcessSupervisor(policy)
    handle = await sup.spawn(_spec(owned_python, workdir, "ignore_interrupt"))
    await asyncio.sleep(0.1)
    await handle.force_terminate(reason="test")
    code = await handle.wait()
    assert code is not None
    # Process should be dead.
    with pytest.raises(ProcessLookupError):
        os.kill(handle.pid, 0)  # type: ignore[arg-type]


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="fork-based descendant test")
async def test_process_tree_termination(owned_python: Path, workdir: Path) -> None:
    policy = RuntimePolicy(terminate_escalation=0.3)
    sup = ProcessSupervisor(policy)
    handle = await sup.spawn(_spec(owned_python, workdir, "spawn_descendant"))
    await asyncio.sleep(0.2)
    pid = handle.pid
    assert pid is not None
    await handle.force_terminate(reason="tree")
    await handle.wait()
    # Entire process group should be gone.
    await asyncio.sleep(0.1)
    # Parent is dead.
    dead = False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        dead = True
    assert dead


@pytest.mark.asyncio
async def test_immutable_launch_snapshot(owned_python: Path, workdir: Path) -> None:
    sup = ProcessSupervisor()
    caps = HarnessCapabilities(kind=HarnessKind.OPENCODE, version="1")
    snap = sup.build_launch_snapshot(
        executable_path=str(owned_python),
        working_directory=str(workdir),
        workspace_roots=(str(workdir),),
        capabilities=caps,
        model="m",
        mode="d",
        adapter_version="a1",
    )
    assert snap.resolved_executable is not None
    # model_copy yields a new immutable snapshot; original is unchanged.
    updated = snap.model_copy(update={"model": "x"})
    assert snap.model == "m"
    assert updated.model == "x"
    assert snap is not updated
    # Direct attribute assignment is rejected on frozen models.
    with pytest.raises(ValidationError):
        snap.model = "x"  # type: ignore[misc]


@pytest.mark.asyncio
async def test_missing_workdir_not_created(owned_python: Path, tmp_path: Path) -> None:
    missing = tmp_path / "gone"
    launch = make_launch(executable=owned_python, workdir=tmp_path)
    # Point working_directory at a non-existent path.
    launch = launch.model_copy(update={"working_directory": str(missing)})
    sup = ProcessSupervisor()
    spec = ProcessSpec(
        conversation_id=uuid4(),
        binding_id=uuid4(),
        process_id=uuid4(),
        launch=launch,
        argv=(str(child_modes_path()), "exit_code", "0"),
    )
    with pytest.raises(DomainError) as ei:
        await sup.spawn(spec)
    assert ei.value.code is ErrorCode.WORKING_DIRECTORY_NOT_FOUND
    assert not missing.exists()


@pytest.mark.asyncio
async def test_late_process_creation_is_terminated(
    owned_python: Path,
    workdir: Path,
) -> None:
    class DelayedSupervisor(ProcessSupervisor):
        def __init__(self) -> None:
            super().__init__(RuntimePolicy(creation_timeout=0.05, terminate_escalation=0.2))
            self.release = asyncio.Event()
            self.created = asyncio.Event()
            self.late_process: asyncio.subprocess.Process | None = None

        async def _create_process(
            self,
            spec: ProcessSpec,
        ) -> tuple[asyncio.subprocess.Process, object | None]:
            await self.release.wait()
            process, job = await super()._create_process(spec)
            self.late_process = process
            self.created.set()
            return process, job

    supervisor = DelayedSupervisor()
    with pytest.raises(DomainError) as exc_info:
        await supervisor.spawn(_spec(owned_python, workdir, "ignore_interrupt"))
    assert exc_info.value.code is ErrorCode.RUNTIME_TIMEOUT
    supervisor.release.set()
    await asyncio.wait_for(supervisor.created.wait(), timeout=2)
    assert supervisor.late_process is not None
    for _ in range(50):
        if supervisor.late_process.returncode is not None:
            break
        await asyncio.sleep(0.02)
    assert supervisor.late_process.returncode is not None


@pytest.mark.asyncio
async def test_windows_assignment_failure_kills_suspended_child(
    monkeypatch: pytest.MonkeyPatch,
    workdir: Path,
) -> None:
    class FakeProcess:
        pid = 123
        killed = False

        def kill(self) -> None:
            self.killed = True

        async def wait(self) -> int:
            return 1

    process = FakeProcess()
    create = AsyncMock(return_value=process)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)

    def open_process(access: int, inherit: bool, pid: int) -> int:
        return 99

    def close_handle(handle: int) -> None:
        return None

    monkeypatch.setitem(
        sys.modules,
        "win32api",
        SimpleNamespace(OpenProcess=open_process, CloseHandle=close_handle),
    )
    monkeypatch.setitem(
        sys.modules,
        "win32con",
        SimpleNamespace(PROCESS_SET_QUOTA=0x100, PROCESS_TERMINATE=0x1),
    )
    from talktoharnesses.runtime import windows_job

    job = object()
    closed: list[object] = []

    def fail_assignment(job_handle: object, process_handle: object) -> None:
        raise OSError("assignment failed")

    def resume(process_handle: object) -> None:
        return None

    monkeypatch.setattr(windows_job, "create_kill_on_close_job", lambda: job)
    monkeypatch.setattr(windows_job, "assign_process_to_job", fail_assignment)
    monkeypatch.setattr(windows_job, "close_job", closed.append)
    monkeypatch.setattr(windows_job, "resume_process", resume)

    with pytest.raises(OSError, match="assignment failed"):
        await ProcessSupervisor()._create_process_windows(  # type: ignore[reportPrivateUsage]
            "program", [], str(workdir)
        )
    assert process.killed
    assert closed == [job]
    assert create.call_args.kwargs["creationflags"] & 0x00000004

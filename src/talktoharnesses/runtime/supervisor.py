"""Secure process supervisor — direct exec, no shell."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from talktoharnesses.application.redaction import StreamingTextRedactor
from talktoharnesses.domain.enums import ErrorCode
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.domain.models import HarnessCapabilities, LaunchSnapshot
from talktoharnesses.runtime.events import ProcessEvent
from talktoharnesses.runtime.handle import ProcessHandle
from talktoharnesses.runtime.paths import resolve_launch_paths
from talktoharnesses.runtime.policy import RuntimePolicy
from talktoharnesses.runtime.spec import ProcessSpec


class ProcessSupervisor:
    """Spawn and supervise harness child processes under a runtime policy."""

    def __init__(
        self,
        policy: RuntimePolicy | None = None,
        *,
        redaction_patterns: Sequence[str] = (),
    ) -> None:
        self.policy = policy or RuntimePolicy()
        self._redaction_patterns = tuple(redaction_patterns)
        self._late_cleanup_tasks: set[asyncio.Task[None]] = set()

    def build_launch_snapshot(
        self,
        *,
        executable_path: str,
        working_directory: str,
        workspace_roots: tuple[str, ...] = (),
        capabilities: HarnessCapabilities,
        model: str | None,
        mode: str | None,
        adapter_version: str,
    ) -> LaunchSnapshot:
        """Resolve paths (no creation) and build an immutable launch snapshot."""
        executable, workdir, roots = resolve_launch_paths(
            executable_path=executable_path,
            working_directory=working_directory,
            workspace_roots=workspace_roots,
        )
        return LaunchSnapshot(
            resolved_executable=str(executable),
            harness_version=capabilities.version,
            working_directory=str(workdir),
            workspace_roots=tuple(str(r) for r in roots),
            model=model,
            mode=mode,
            adapter_version=adapter_version,
            capabilities=capabilities,
        )

    async def spawn(
        self,
        spec: ProcessSpec,
        *,
        on_lifecycle: Callable[[ProcessEvent], None] | None = None,
        redaction_patterns: Sequence[str] | None = None,
    ) -> ProcessHandle:
        """Launch ``resolved_executable + argv`` under creation timeout."""
        launch = spec.launch
        if not launch.resolved_executable:
            raise DomainError(
                ErrorCode.INVALID_EXECUTABLE,
                "launch snapshot missing resolved_executable",
            )
        executable = Path(launch.resolved_executable)
        # Re-validate security on the resolved path at spawn time.
        from talktoharnesses.runtime.paths import resolve_directory, resolve_executable

        resolve_executable(str(executable))
        resolve_directory(
            launch.working_directory,
            error_code=ErrorCode.WORKING_DIRECTORY_NOT_FOUND,
        )
        for root in launch.workspace_roots:
            resolve_directory(root, error_code=ErrorCode.WORKSPACE_ROOT_NOT_FOUND)

        patterns = (
            self._redaction_patterns if redaction_patterns is None else tuple(redaction_patterns)
        )
        redactor = StreamingTextRedactor(patterns)

        creation = asyncio.create_task(
            self._create_process(spec),
            name=f"create-process-{spec.process_id}",
        )
        try:
            process, job = await asyncio.wait_for(
                asyncio.shield(creation),
                timeout=self.policy.creation_timeout,
            )
        except TimeoutError as exc:
            self._schedule_late_cleanup(creation)
            raise DomainError(
                ErrorCode.RUNTIME_TIMEOUT,
                "process creation timed out",
                details={"process_id": str(spec.process_id)},
            ) from exc
        except asyncio.CancelledError:
            self._schedule_late_cleanup(creation)
            raise

        return ProcessHandle(
            process_id=spec.process_id,
            process=process,
            policy=self.policy,
            redactor=redactor,
            job=job,
            on_lifecycle=on_lifecycle,
        )

    def _schedule_late_cleanup(
        self,
        creation: asyncio.Task[tuple[asyncio.subprocess.Process, object | None]],
    ) -> None:
        cleanup = asyncio.create_task(
            self._cleanup_late_creation(creation),
            name="late-process-cleanup",
        )
        self._late_cleanup_tasks.add(cleanup)
        cleanup.add_done_callback(self._late_cleanup_tasks.discard)

    async def _cleanup_late_creation(
        self,
        creation: asyncio.Task[tuple[asyncio.subprocess.Process, object | None]],
    ) -> None:
        try:
            process, job = await creation
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001
            return
        if job is not None and sys.platform == "win32":
            from talktoharnesses.runtime.windows_job import close_job, terminate_job

            terminate_job(job, 1)
            close_job(job)
        elif process.returncode is None:
            if sys.platform == "win32":
                with contextlib.suppress(ProcessLookupError, OSError):
                    process.kill()
            else:
                with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                    os.killpg(process.pid, signal.SIGKILL)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(process.wait(), timeout=self.policy.terminate_escalation)

    async def _create_process(
        self,
        spec: ProcessSpec,
    ) -> tuple[asyncio.subprocess.Process, object | None]:
        assert spec.launch.resolved_executable is not None
        program = spec.launch.resolved_executable
        args = list(spec.argv)
        cwd = spec.launch.working_directory

        if sys.platform == "win32":
            return await self._create_process_windows(program, args, cwd)
        process = await asyncio.create_subprocess_exec(
            program,
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            start_new_session=True,
        )
        return process, None

    async def _create_process_windows(
        self,
        program: str,
        args: list[str],
        cwd: str,
    ) -> tuple[asyncio.subprocess.Process, object]:
        """Create suspended process, attach kill-on-close Job Object, resume.

        Pass CREATE_SUSPENDED through asyncio's creation flags, attach the Job
        Object, and resume only after assignment succeeds.
        """
        import subprocess

        from talktoharnesses.runtime.windows_job import (
            assign_process_to_job,
            close_job,
            create_kill_on_close_job,
            resume_process,
        )

        creationflags = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)) | int(
            getattr(subprocess, "CREATE_SUSPENDED", 0x00000004)
        )
        process = await asyncio.create_subprocess_exec(
            program,
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            creationflags=creationflags,
        )
        job: object | None = None
        try:
            import win32api  # type: ignore[import-untyped]
            import win32con  # type: ignore[import-untyped]

            job = create_kill_on_close_job()
            access = win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE | 0x0800
            handle = win32api.OpenProcess(access, False, process.pid)
            try:
                assign_process_to_job(job, handle)
                resume_process(handle)
            finally:
                win32api.CloseHandle(handle)
        except Exception:
            # A suspended child must never escape supervision.
            if job is not None:
                close_job(job)
            with contextlib.suppress(ProcessLookupError, OSError):
                process.kill()
            with contextlib.suppress(Exception):
                await process.wait()
            raise
        assert job is not None
        return process, job

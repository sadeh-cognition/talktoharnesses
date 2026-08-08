"""Windows Job Object helpers for kill-on-close process trees."""

from __future__ import annotations

import contextlib
import ctypes
import sys
from typing import Any

# Typed loosely: pywin32 is Windows-only and may be absent on other platforms.
_HANDLE = Any


def create_kill_on_close_job() -> _HANDLE:
    """Create a Job Object that kills assigned processes when the job is closed."""
    if sys.platform != "win32":  # pragma: no cover
        msg = "Job Objects are Windows-only"
        raise RuntimeError(msg)

    import win32api  # type: ignore[import-untyped]
    import win32job  # type: ignore[import-untyped]

    job = win32job.CreateJobObject(None, "")
    info = win32job.QueryInformationJobObject(
        job,
        win32job.JobObjectExtendedLimitInformation,
    )
    info["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    win32job.SetInformationJobObject(
        job,
        win32job.JobObjectExtendedLimitInformation,
        info,
    )
    # Keep a reference so the handle is not GC'd unexpectedly; caller owns it.
    _ = win32api
    return job


def assign_process_to_job(job: _HANDLE, process_handle: _HANDLE) -> None:
    if sys.platform != "win32":  # pragma: no cover
        msg = "Job Objects are Windows-only"
        raise RuntimeError(msg)
    import win32job  # type: ignore[import-untyped]

    win32job.AssignProcessToJobObject(job, process_handle)


def resume_process(process_handle: _HANDLE) -> None:
    """Resume a process created with CREATE_SUSPENDED after job assignment."""
    if sys.platform != "win32":  # pragma: no cover
        msg = "process suspension is Windows-only"
        raise RuntimeError(msg)
    status = ctypes.windll.ntdll.NtResumeProcess(int(process_handle))  # type: ignore[attr-defined]
    if status != 0:
        raise OSError(status, "NtResumeProcess failed")


def terminate_job(job: _HANDLE, exit_code: int = 1) -> None:
    if sys.platform != "win32":  # pragma: no cover
        return
    import win32job  # type: ignore[import-untyped]

    with contextlib.suppress(Exception):
        win32job.TerminateJobObject(job, exit_code)


def close_job(job: _HANDLE) -> None:
    if sys.platform != "win32":  # pragma: no cover
        return
    import win32api  # type: ignore[import-untyped]

    with contextlib.suppress(Exception):
        win32api.CloseHandle(job)

"""Windows Job Object helpers — exercise win32 branches via stubs."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from talktoharnesses.runtime import windows_job


def test_non_windows_guards() -> None:
    with pytest.raises(RuntimeError):
        windows_job.create_kill_on_close_job()
    with pytest.raises(RuntimeError):
        windows_job.assign_process_to_job(object(), object())
    with pytest.raises(RuntimeError):
        windows_job.resume_process(object())
    windows_job.terminate_job(object())
    windows_job.close_job(object())


def test_windows_job_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    class FakeWin32Job:
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000

        JobObjectExtendedLimitInformation = 9

        @staticmethod
        def CreateJobObject(_sa: object, _name: str) -> object:
            calls.append("create")
            return object()

        @staticmethod
        def QueryInformationJobObject(_job: object, _info: object) -> dict[str, dict[str, int]]:
            return {"BasicLimitInformation": {"LimitFlags": 0}}

        @staticmethod
        def SetInformationJobObject(_job: object, _info: object, data: object) -> None:
            calls.append("set")
            assert isinstance(data, dict)

        @staticmethod
        def AssignProcessToJobObject(_job: object, _proc: object) -> None:
            calls.append("assign")

        @staticmethod
        def TerminateJobObject(_job: object, _code: int) -> None:
            calls.append("terminate")

    class FakeWin32Api:
        @staticmethod
        def CloseHandle(_handle: object) -> None:
            calls.append("close")

    monkeypatch.setattr(windows_job.sys, "platform", "win32")
    monkeypatch.setitem(__import__("sys").modules, "win32job", FakeWin32Job)
    monkeypatch.setitem(__import__("sys").modules, "win32api", FakeWin32Api)

    job = windows_job.create_kill_on_close_job()
    windows_job.assign_process_to_job(job, object())
    windows_job.terminate_job(job, 7)
    windows_job.close_job(job)
    assert "create" in calls and "set" in calls and "assign" in calls
    assert "terminate" in calls and "close" in calls


def test_resume_process_success_and_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(windows_job.sys, "platform", "win32")

    class FakeNtdll:
        def __init__(self) -> None:
            self.calls: list[int] = []

        def NtResumeProcess(self, handle: int) -> int:
            self.calls.append(handle)
            return 0 if handle == 1 else 0xC0000001

    fake = FakeNtdll()
    monkeypatch.setattr(
        windows_job.ctypes,
        "windll",
        SimpleNamespace(ntdll=fake),
        raising=False,
    )
    # ctypes.windll may not exist on Linux; patch module attribute access path
    monkeypatch.setattr(
        windows_job,
        "ctypes",
        SimpleNamespace(windll=SimpleNamespace(ntdll=fake)),
    )
    windows_job.resume_process(1)
    with pytest.raises(OSError):
        windows_job.resume_process(2)

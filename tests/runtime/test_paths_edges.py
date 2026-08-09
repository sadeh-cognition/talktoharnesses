"""Additional runtime path resolution edge coverage."""

from __future__ import annotations

from pathlib import Path

import pytest

from talktoharnesses.domain.enums import ErrorCode
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.runtime import paths as paths_mod


def test_resolve_directory_rejects_missing_and_file(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    with pytest.raises(DomainError) as exc:
        paths_mod.resolve_directory(str(missing), error_code=ErrorCode.INVALID_STATE)
    assert exc.value.code is ErrorCode.INVALID_STATE

    file_path = tmp_path / "file.txt"
    file_path.write_text("x", encoding="utf-8")
    with pytest.raises(DomainError) as not_dir:
        paths_mod.resolve_directory(str(file_path), error_code=ErrorCode.INVALID_STATE)
    assert not_dir.value.code is ErrorCode.INVALID_STATE


def test_resolve_executable_rejects_missing_and_directory(tmp_path: Path) -> None:
    with pytest.raises(DomainError) as missing:
        paths_mod.resolve_executable(str(tmp_path / "missing-bin"))
    assert missing.value.code is ErrorCode.INVALID_EXECUTABLE

    with pytest.raises(DomainError) as directory:
        paths_mod.resolve_executable(str(tmp_path))
    assert directory.value.code is ErrorCode.INVALID_EXECUTABLE


def test_effective_access_fallback_and_windows_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exe = tmp_path / "tool"
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    exe.chmod(0o755)

    def boom(*_args: object, **_kwargs: object) -> bool:
        raise NotImplementedError

    monkeypatch.setattr(paths_mod.os, "access", boom)
    assert paths_mod._effective_access(exe) is True  # pyright: ignore[reportPrivateUsage]

    monkeypatch.setattr(paths_mod.sys, "platform", "win32")

    def access_ok(path: object, mode: object) -> bool:
        del path, mode
        return True

    monkeypatch.setattr(paths_mod.os, "access", access_ok)
    assert paths_mod._effective_access(exe) is True  # pyright: ignore[reportPrivateUsage]


def test_windows_ownership_import_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exe = tmp_path / "tool"
    exe.write_text("x", encoding="utf-8")
    monkeypatch.setattr(paths_mod.sys, "platform", "win32")

    def raise_import(_path: Path) -> tuple[object, object]:
        raise ImportError("no pywin32")

    monkeypatch.setattr(paths_mod, "_windows_file_and_token_sids", raise_import)
    with pytest.raises(DomainError) as exc:
        paths_mod._check_ownership_windows(exe)  # pyright: ignore[reportPrivateUsage]
    assert exc.value.code is ErrorCode.INVALID_EXECUTABLE


def test_effective_access_group_and_other_bits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exe = tmp_path / "tool"
    exe.write_text("x", encoding="utf-8")

    class Stat:
        def __init__(self, mode: int, uid: int, gid: int) -> None:
            self.st_mode = mode
            self.st_uid = uid
            self.st_gid = gid

    def boom(*_a: object, **_k: object) -> bool:
        raise TypeError("no effective_ids")

    def stat_group(_self: Path) -> Stat:
        return Stat(0o010, uid=2, gid=20)

    def stat_other(_self: Path) -> Stat:
        return Stat(0o001, uid=2, gid=30)

    def empty_groups() -> list[int]:
        return []

    monkeypatch.setattr(paths_mod.os, "access", boom)
    monkeypatch.setattr(paths_mod.os, "geteuid", lambda: 1)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(paths_mod.os, "getegid", lambda: 20)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(paths_mod.os, "getgroups", lambda: [20])  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    monkeypatch.setattr(Path, "stat", stat_group)
    assert paths_mod._effective_access(exe) is True  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(Path, "stat", stat_other)
    monkeypatch.setattr(paths_mod.os, "getgroups", empty_groups)
    assert paths_mod._effective_access(exe) is True  # pyright: ignore[reportPrivateUsage]


def test_windows_ownership_helpers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exe = tmp_path / "tool"
    exe.write_text("x", encoding="utf-8")
    monkeypatch.setattr(paths_mod.sys, "platform", "win32")

    class FakeSec:
        OWNER_SECURITY_INFORMATION = 1
        TOKEN_QUERY = 8
        TokenUser = 1

        @staticmethod
        def GetFileSecurity(_path: str, _info: object) -> object:
            return SimpleNamespace(
                GetSecurityDescriptorOwner=lambda: "owner-sid",
            )

        @staticmethod
        def OpenProcessToken(_proc: object, _access: object) -> object:
            return "token"

        @staticmethod
        def GetTokenInformation(_token: object, _cls: object) -> tuple[str]:
            return ("owner-sid",)

    class FakeApi:
        @staticmethod
        def GetCurrentProcess() -> object:
            return "proc"

        @staticmethod
        def CloseHandle(_handle: object) -> None:
            return None

    import sys
    from types import SimpleNamespace

    monkeypatch.setitem(sys.modules, "win32security", FakeSec)
    monkeypatch.setitem(sys.modules, "win32api", FakeApi)
    owner, user = paths_mod._windows_file_and_token_sids(exe)  # pyright: ignore[reportPrivateUsage]
    assert owner == user == "owner-sid"
    paths_mod._check_ownership_windows(exe)  # pyright: ignore[reportPrivateUsage]

    def mismatch_sids(_p: Path) -> tuple[str, str]:
        return ("a", "b")

    monkeypatch.setattr(
        paths_mod,
        "_windows_file_and_token_sids",
        mismatch_sids,
    )
    with pytest.raises(DomainError) as mismatch:
        paths_mod._check_ownership_windows(exe)  # pyright: ignore[reportPrivateUsage]
    assert mismatch.value.code is ErrorCode.EXECUTABLE_OWNER_MISMATCH

    monkeypatch.setattr(paths_mod.sys, "platform", "win32")

    def boom_sids(_p: Path) -> tuple[str, str]:
        raise RuntimeError("boom")

    monkeypatch.setattr(
        paths_mod,
        "_windows_file_and_token_sids",
        boom_sids,
    )
    with pytest.raises(DomainError) as generic:
        paths_mod._check_ownership_windows(exe)  # pyright: ignore[reportPrivateUsage]
    assert generic.value.code is ErrorCode.INVALID_EXECUTABLE


def test_check_ownership_dispatches_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exe = tmp_path / "tool"
    exe.write_text("x", encoding="utf-8")
    called: list[Path] = []
    monkeypatch.setattr(paths_mod.sys, "platform", "win32")

    def record_windows(path: Path) -> None:
        called.append(path)

    monkeypatch.setattr(
        paths_mod,
        "_check_ownership_windows",
        record_windows,
    )
    paths_mod._check_ownership(exe)  # pyright: ignore[reportPrivateUsage]
    assert called == [exe]


def test_ownership_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exe = tmp_path / "tool"
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    exe.chmod(0o755)

    class Stat:
        st_uid = 0
        st_mode = 0o100755
        st_gid = 0

    def stat_mismatch(self: Path) -> Stat:
        return Stat()

    monkeypatch.setattr(Path, "stat", stat_mismatch)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(paths_mod.os, "geteuid", lambda: 1)
    with pytest.raises(DomainError) as exc:
        paths_mod._check_ownership(exe)  # pyright: ignore[reportPrivateUsage]
    assert exc.value.code is ErrorCode.EXECUTABLE_OWNER_MISMATCH

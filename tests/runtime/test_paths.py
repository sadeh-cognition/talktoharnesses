"""Path resolution and ownership security checks."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from talktoharnesses.domain import DomainError, ErrorCode
from talktoharnesses.runtime.paths import (
    resolve_directory,
    resolve_executable,
    resolve_launch_paths,
)


def test_resolve_directory_missing(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    with pytest.raises(DomainError) as ei:
        resolve_directory(str(missing), error_code=ErrorCode.WORKING_DIRECTORY_NOT_FOUND)
    assert ei.value.code is ErrorCode.WORKING_DIRECTORY_NOT_FOUND


def test_resolve_directory_file_not_dir(tmp_path: Path) -> None:
    f = tmp_path / "file"
    f.write_text("x")
    with pytest.raises(DomainError) as ei:
        resolve_directory(str(f), error_code=ErrorCode.WORKSPACE_ROOT_NOT_FOUND)
    assert ei.value.code is ErrorCode.WORKSPACE_ROOT_NOT_FOUND


def test_resolve_directory_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    resolved = resolve_directory(str(link), error_code=ErrorCode.WORKING_DIRECTORY_NOT_FOUND)
    assert resolved == real.resolve()


def test_resolve_directory_does_not_create(tmp_path: Path) -> None:
    missing = tmp_path / "a" / "b"
    with pytest.raises(DomainError):
        resolve_directory(str(missing), error_code=ErrorCode.WORKING_DIRECTORY_NOT_FOUND)
    assert not missing.exists()


def test_invalid_executable_missing(tmp_path: Path) -> None:
    with pytest.raises(DomainError) as ei:
        resolve_executable(str(tmp_path / "missing-bin"))
    assert ei.value.code is ErrorCode.INVALID_EXECUTABLE


def test_invalid_executable_not_file(tmp_path: Path) -> None:
    d = tmp_path / "dir"
    d.mkdir()
    with pytest.raises(DomainError) as ei:
        resolve_executable(str(d))
    assert ei.value.code is ErrorCode.INVALID_EXECUTABLE


def test_invalid_executable_not_executable(tmp_path: Path) -> None:
    f = tmp_path / "script"
    f.write_text("#!/bin/sh\n")
    f.chmod(0o644)
    with pytest.raises(DomainError) as ei:
        resolve_executable(str(f))
    assert ei.value.code is ErrorCode.INVALID_EXECUTABLE


def test_executable_owner_mismatch(owned_python: Path) -> None:
    # Force euid mismatch without needing another OS user.
    with (
        patch("talktoharnesses.runtime.paths.os.geteuid", return_value=os.geteuid() + 1),
        pytest.raises(DomainError) as ei,
    ):
        resolve_executable(str(owned_python))
    assert ei.value.code is ErrorCode.EXECUTABLE_OWNER_MISMATCH


def test_effective_access_fallback_and_stat_failure(tmp_path: Path, owned_python: Path) -> None:
    from talktoharnesses.runtime import paths as paths_mod

    def _access_raises(*_a: object, **_k: object) -> bool:
        raise TypeError("no effective_ids")

    with patch.object(paths_mod.os, "access", side_effect=_access_raises):
        assert paths_mod._effective_access(owned_python) is True  # pyright: ignore[reportPrivateUsage]

    with (
        patch.object(Path, "stat", side_effect=OSError("gone")),
        pytest.raises(DomainError) as ei,
    ):
        paths_mod._check_ownership(tmp_path / "x")  # pyright: ignore[reportPrivateUsage]
    assert ei.value.code is ErrorCode.INVALID_EXECUTABLE

    with (
        patch("talktoharnesses.runtime.paths.sys.platform", "win32"),
        patch(
            "talktoharnesses.runtime.paths._windows_file_and_token_sids",
            side_effect=ImportError("no pywin32"),
        ),
        pytest.raises(DomainError) as win,
    ):
        paths_mod._check_ownership_windows(owned_python)  # pyright: ignore[reportPrivateUsage]
    assert win.value.code is ErrorCode.INVALID_EXECUTABLE

    with (
        patch("talktoharnesses.runtime.paths.sys.platform", "win32"),
        patch(
            "talktoharnesses.runtime.paths._windows_file_and_token_sids",
            side_effect=RuntimeError("acl boom"),
        ),
        pytest.raises(DomainError) as win2,
    ):
        paths_mod._check_ownership_windows(owned_python)  # pyright: ignore[reportPrivateUsage]
    assert win2.value.code is ErrorCode.INVALID_EXECUTABLE

    with (
        patch("talktoharnesses.runtime.paths.sys.platform", "win32"),
        patch(
            "talktoharnesses.runtime.paths._windows_file_and_token_sids",
            return_value=("owner", "other"),
        ),
        pytest.raises(DomainError) as mismatch,
    ):
        paths_mod._check_ownership_windows(owned_python)  # pyright: ignore[reportPrivateUsage]
    assert mismatch.value.code is ErrorCode.EXECUTABLE_OWNER_MISMATCH


def test_resolve_launch_paths_ok(workdir: Path, owned_python: Path) -> None:
    exe, wd, roots = resolve_launch_paths(
        executable_path=str(owned_python),
        working_directory=str(workdir),
        workspace_roots=(str(workdir),),
    )
    assert exe.is_file()
    assert wd.is_dir()
    assert roots[0].is_dir()


@pytest.mark.skipif(os.name == "nt", reason="effective_ids is Unix-only")
def test_execute_check_uses_effective_ids(owned_python: Path) -> None:
    real_access = os.access

    def access(path: object, mode: int, *, effective_ids: bool = False) -> bool:
        assert effective_ids
        return real_access(path, mode, effective_ids=True)  # type: ignore[arg-type]

    with patch("talktoharnesses.runtime.paths.os.access", side_effect=access):
        assert resolve_executable(str(owned_python)) == owned_python.resolve()

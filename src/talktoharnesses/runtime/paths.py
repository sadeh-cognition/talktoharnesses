"""Strict path resolution for executables and workspace roots (no creation)."""

from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path
from typing import Any

from talktoharnesses.domain.enums import ErrorCode
from talktoharnesses.domain.errors import DomainError


def resolve_directory(path: str, *, error_code: ErrorCode) -> Path:
    """Resolve symlinks strictly; require an existing directory. Never create."""
    try:
        resolved = Path(path).resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise DomainError(
            error_code,
            f"directory not found: {path}",
            details={"path": path},
        ) from exc
    if not resolved.is_dir():
        raise DomainError(
            error_code,
            f"path is not a directory: {path}",
            details={"path": path, "resolved": str(resolved)},
        )
    return resolved


def resolve_executable(path: str) -> Path:
    """Resolve executable symlinks; require a regular owned executable file."""
    try:
        resolved = Path(path).resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise DomainError(
            ErrorCode.INVALID_EXECUTABLE,
            f"executable not found: {path}",
            details={"path": path},
        ) from exc

    if not resolved.is_file():
        raise DomainError(
            ErrorCode.INVALID_EXECUTABLE,
            f"executable is not a regular file: {path}",
            details={"path": path, "resolved": str(resolved)},
        )

    if not _effective_access(resolved):
        raise DomainError(
            ErrorCode.INVALID_EXECUTABLE,
            f"executable is not executable: {path}",
            details={"path": path, "resolved": str(resolved)},
        )

    _check_ownership(resolved)
    return resolved


def _effective_access(path: Path) -> bool:
    """Check execute permission using the credentials used for ownership."""
    if sys.platform == "win32":
        return os.access(path, os.X_OK)
    try:
        return os.access(path, os.X_OK, effective_ids=True)
    except (NotImplementedError, TypeError):
        st = path.stat()
        mode = st.st_mode
        if st.st_uid == os.geteuid():
            return bool(mode & 0o100)
        if st.st_gid == os.getegid() or st.st_gid in os.getgroups():
            return bool(mode & 0o010)
        return bool(mode & 0o001)


def _check_ownership(path: Path) -> None:
    if sys.platform == "win32":
        _check_ownership_windows(path)
        return
    try:
        st = path.stat()
    except OSError as exc:
        raise DomainError(
            ErrorCode.INVALID_EXECUTABLE,
            f"cannot stat executable: {path}",
            details={"path": str(path)},
        ) from exc
    if st.st_uid != os.geteuid():
        raise DomainError(
            ErrorCode.EXECUTABLE_OWNER_MISMATCH,
            f"executable owner does not match effective UID: {path}",
            details={
                "path": str(path),
                "owner_uid": st.st_uid,
                "euid": os.geteuid(),
            },
        )


def _windows_file_and_token_sids(path: Path) -> tuple[Any, Any]:
    """Return (file_owner_sid, current_user_sid). Isolated for pywin32 typing."""
    import win32api  # type: ignore[import-not-found,import-untyped]
    import win32security  # type: ignore[import-not-found,import-untyped]

    api: Any = win32api
    sec: Any = win32security
    sd = sec.GetFileSecurity(str(path), sec.OWNER_SECURITY_INFORMATION)
    owner_sid = sd.GetSecurityDescriptorOwner()
    process = api.GetCurrentProcess()
    token = sec.OpenProcessToken(process, sec.TOKEN_QUERY)
    try:
        user_sid = sec.GetTokenInformation(token, sec.TokenUser)[0]
    finally:
        with contextlib.suppress(Exception):
            api.CloseHandle(token)
    return owner_sid, user_sid


def _check_ownership_windows(path: Path) -> None:
    """Compare current-token SID to the file owner SID via pywin32."""
    try:
        owner_sid, user_sid = _windows_file_and_token_sids(path)
    except ImportError as exc:  # pragma: no cover - Windows-only dependency
        raise DomainError(
            ErrorCode.INVALID_EXECUTABLE,
            "pywin32 is required for Windows executable ownership checks",
            details={"path": str(path)},
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise DomainError(
            ErrorCode.INVALID_EXECUTABLE,
            f"cannot read Windows file ownership: {path}",
            details={"path": str(path)},
        ) from exc

    if owner_sid != user_sid:
        raise DomainError(
            ErrorCode.EXECUTABLE_OWNER_MISMATCH,
            f"executable owner SID does not match current token: {path}",
            details={"path": str(path)},
        )


def resolve_launch_paths(
    *,
    executable_path: str,
    working_directory: str,
    workspace_roots: tuple[str, ...],
) -> tuple[Path, Path, tuple[Path, ...]]:
    """Resolve executable, working directory, and all workspace roots."""
    executable = resolve_executable(executable_path)
    workdir = resolve_directory(
        working_directory,
        error_code=ErrorCode.WORKING_DIRECTORY_NOT_FOUND,
    )
    roots: list[Path] = []
    for root in workspace_roots:
        roots.append(resolve_directory(root, error_code=ErrorCode.WORKSPACE_ROOT_NOT_FOUND))
    return executable, workdir, tuple(roots)

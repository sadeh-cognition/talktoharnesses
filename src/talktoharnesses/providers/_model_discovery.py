"""Shared subprocess runner for provider model discovery commands."""

from __future__ import annotations

import asyncio
from pathlib import Path

from talktoharnesses.domain.enums import ErrorCode
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.runtime.paths import resolve_directory


async def run_model_command(
    executable: Path,
    *args: str,
    provider: str,
    working_directory: str,
) -> str:
    resolved_working_directory = resolve_directory(
        working_directory,
        error_code=ErrorCode.WORKING_DIRECTORY_NOT_FOUND,
    )
    try:
        process = await asyncio.create_subprocess_exec(
            str(executable),
            *args,
            cwd=resolved_working_directory,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise DomainError(
            ErrorCode.INVALID_EXECUTABLE,
            f"failed to discover {provider} models: {exc}",
            details={"executable": str(executable)},
        ) from exc
    stdout, stderr = await process.communicate()
    if process.returncode not in (0, None):
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            f"{provider} model discovery failed",
            details={
                "returncode": process.returncode,
                "stderr": stderr.decode("utf-8", errors="replace")[:500],
            },
        )
    return stdout.decode("utf-8", errors="replace")

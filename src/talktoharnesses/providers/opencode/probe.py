"""OpenCode version probe against the packaged compatibility source."""

from __future__ import annotations

import asyncio
import sys

from talktoharnesses.domain.enums import ErrorCode
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.domain.models import (
    HarnessCapabilities,
    HarnessConfiguration,
    HarnessModelInfo,
)
from talktoharnesses.providers._model_discovery import run_model_command
from talktoharnesses.providers.opencode.compatibility import (
    OpenCodeReleaseRecord,
    match_release,
)
from talktoharnesses.runtime.paths import resolve_executable


async def probe_opencode(
    config: HarnessConfiguration,
) -> tuple[HarnessCapabilities, OpenCodeReleaseRecord]:
    if not config.executable_path:
        raise DomainError(ErrorCode.INVALID_EXECUTABLE, "configuration has no executable_path")
    executable = resolve_executable(config.executable_path)
    try:
        proc = await asyncio.create_subprocess_exec(
            str(executable),
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise DomainError(
            ErrorCode.INVALID_EXECUTABLE,
            f"failed to execute opencode: {exc}",
            details={"executable": str(executable)},
        ) from exc
    stdout_b, stderr_b = await proc.communicate()
    if proc.returncode not in (0, None):
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            "opencode --version failed",
            details={
                "returncode": proc.returncode,
                "stderr": stderr_b.decode("utf-8", errors="replace")[:500],
            },
        )
    version_stdout = stdout_b.decode("utf-8", errors="replace")
    release = match_release(version_stdout, platform=sys.platform)
    output = await run_model_command(
        executable,
        "models",
        provider="OpenCode",
        working_directory=config.working_directory,
    )
    models = _parse_models(output)
    capabilities = release.to_harness_capabilities().model_copy(update={"models": models})
    return capabilities, release


def _parse_models(output: str) -> tuple[HarnessModelInfo, ...]:
    models: list[HarnessModelInfo] = []
    for raw_line in output.splitlines():
        model_id = raw_line.strip()
        if not model_id:
            continue
        if any(char.isspace() for char in model_id) or "/" not in model_id:
            raise DomainError(
                ErrorCode.PROVIDER_INCOMPATIBLE,
                "malformed OpenCode model list",
            )
        models.append(HarnessModelInfo(id=model_id))
    if not models:
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            "OpenCode advertised no models",
        )
    return tuple(models)

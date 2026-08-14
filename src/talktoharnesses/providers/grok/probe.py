"""Grok version probe against the packaged compatibility source."""

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
from talktoharnesses.providers.grok.compatibility import (
    GrokReleaseRecord,
    match_release,
)
from talktoharnesses.runtime.paths import resolve_executable


async def probe_grok(config: HarnessConfiguration) -> tuple[HarnessCapabilities, GrokReleaseRecord]:
    """Run ``grok --version``, match compatibility, return capabilities + release."""
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
            f"failed to execute grok: {exc}",
            details={"executable": str(executable)},
        ) from exc
    stdout_b, stderr_b = await proc.communicate()
    if proc.returncode not in (0, None):
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            "grok --version failed",
            details={
                "returncode": proc.returncode,
                "stderr": stderr_b.decode("utf-8", errors="replace")[:500],
            },
        )
    version_stdout = stdout_b.decode("utf-8", errors="replace")
    release = match_release(version_stdout, platform=sys.platform)
    models = _parse_models(
        await run_model_command(
            executable,
            "models",
            provider="Grok",
            working_directory=config.working_directory,
        )
    )
    capabilities = release.to_harness_capabilities().model_copy(update={"models": models})
    return capabilities, release


def _parse_models(output: str) -> tuple[HarnessModelInfo, ...]:
    _, separator, rows = output.partition("Available models:")
    if not separator:
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            "malformed Grok model list",
        )
    models: list[HarnessModelInfo] = []
    for raw_line in rows.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if not line.startswith(("* ", "- ")):
            break
        model_id = line[2:].removesuffix(" (default)").strip()
        if not model_id or any(char.isspace() for char in model_id):
            raise DomainError(
                ErrorCode.PROVIDER_INCOMPATIBLE,
                "malformed Grok model list",
            )
        models.append(HarnessModelInfo(id=model_id))
    if not models:
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            "Grok advertised no models",
        )
    return tuple(models)

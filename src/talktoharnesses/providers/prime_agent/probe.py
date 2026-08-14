"""Prime Agent version probe."""

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
from talktoharnesses.providers.effort import validate_effort
from talktoharnesses.providers.prime_agent.compatibility import (
    PrimeAgentReleaseRecord,
    match_release,
)
from talktoharnesses.runtime.paths import resolve_executable


async def probe_prime_agent(
    config: HarnessConfiguration,
) -> tuple[HarnessCapabilities, PrimeAgentReleaseRecord]:
    if config.mode is not None:
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            "Prime Agent mode no longer represents thinking; recreate the harness with effort",
            details={"mode": config.mode},
        )
    if not config.executable_path:
        raise DomainError(ErrorCode.INVALID_EXECUTABLE, "configuration has no executable_path")
    executable = resolve_executable(config.executable_path)
    try:
        process = await asyncio.create_subprocess_exec(
            str(executable),
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise DomainError(
            ErrorCode.INVALID_EXECUTABLE,
            f"failed to execute prime-agent: {exc}",
            details={"executable": str(executable)},
        ) from exc
    stdout, stderr = await process.communicate()
    if process.returncode not in (0, None):
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            "prime-agent --version failed",
            details={
                "returncode": process.returncode,
                "stderr": stderr.decode("utf-8", errors="replace")[:500],
            },
        )
    version_output = stdout if stdout.strip() else stderr
    release = match_release(version_output.decode("utf-8", errors="replace"), platform=sys.platform)
    output = await run_model_command(
        executable,
        "model",
        "list",
        provider="Prime Agent",
        working_directory=config.working_directory,
    )
    models = _parse_models(output)
    capabilities = release.to_harness_capabilities().model_copy(update={"models": models})
    validate_effort(config, capabilities)
    return capabilities, release


def _parse_models(output: str) -> tuple[HarnessModelInfo, ...]:
    lines = [line for line in output.splitlines() if line.strip()]
    if not lines or lines[0].split()[:2] != ["provider", "model"]:
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            "malformed Prime Agent model list",
        )
    models: list[HarnessModelInfo] = []
    for line in lines[1:]:
        columns = line.split()
        if len(columns) != 6:
            raise DomainError(
                ErrorCode.PROVIDER_INCOMPATIBLE,
                "malformed Prime Agent model list",
            )
        provider, model_id = columns[:2]
        models.append(HarnessModelInfo(id=f"{provider}/{model_id}", label=model_id))
    if not models:
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            "Prime Agent advertised no models",
        )
    return tuple(models)

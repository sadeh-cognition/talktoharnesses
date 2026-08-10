"""Prime Agent version probe."""

from __future__ import annotations

import asyncio
import sys

from talktoharnesses.domain.enums import ErrorCode
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.domain.models import HarnessCapabilities, HarnessConfiguration
from talktoharnesses.providers.prime_agent.compatibility import (
    PrimeAgentReleaseRecord,
    match_release,
)
from talktoharnesses.runtime.paths import resolve_executable


async def probe_prime_agent(
    config: HarnessConfiguration,
) -> tuple[HarnessCapabilities, PrimeAgentReleaseRecord]:
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
    return release.to_harness_capabilities(), release

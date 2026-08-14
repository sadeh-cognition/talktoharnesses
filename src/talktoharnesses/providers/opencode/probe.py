"""OpenCode version probe against the packaged compatibility source."""

from __future__ import annotations

import asyncio
import json
import sys
from typing import cast

from talktoharnesses.domain.enums import ErrorCode
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.domain.models import (
    HarnessCapabilities,
    HarnessConfiguration,
    HarnessEffortInfo,
    HarnessModelInfo,
)
from talktoharnesses.providers._model_discovery import run_model_command
from talktoharnesses.providers.effort import validate_effort
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
        "--verbose",
        provider="OpenCode",
        working_directory=config.working_directory,
    )
    models = _parse_models(output)
    capabilities = release.to_harness_capabilities().model_copy(update={"models": models})
    validate_effort(config, capabilities)
    return capabilities, release


def _parse_models(output: str) -> tuple[HarnessModelInfo, ...]:
    decoder = json.JSONDecoder()
    models: list[HarnessModelInfo] = []
    index = 0
    while index < len(output):
        while index < len(output) and output[index].isspace():
            index += 1
        if index >= len(output):
            break
        line_end = output.find("\n", index)
        if line_end < 0:
            raise DomainError(
                ErrorCode.PROVIDER_INCOMPATIBLE,
                "malformed OpenCode verbose model list",
            )
        model_id = output[index:line_end].strip()
        if any(char.isspace() for char in model_id) or "/" not in model_id:
            raise DomainError(
                ErrorCode.PROVIDER_INCOMPATIBLE,
                "malformed OpenCode verbose model list",
            )
        index = line_end + 1
        while index < len(output) and output[index].isspace():
            index += 1
        try:
            metadata, index = decoder.raw_decode(output, index)
        except (json.JSONDecodeError, TypeError) as exc:
            raise DomainError(
                ErrorCode.PROVIDER_INCOMPATIBLE,
                "malformed OpenCode verbose model metadata",
            ) from exc
        if not isinstance(metadata, dict):
            raise DomainError(
                ErrorCode.PROVIDER_INCOMPATIBLE,
                "malformed OpenCode verbose model metadata",
            )
        values = cast(dict[object, object], metadata)
        variants = values.get("variants")
        if variants is None:
            variants = {}
        if not isinstance(variants, dict):
            raise DomainError(
                ErrorCode.PROVIDER_INCOMPATIBLE,
                "malformed OpenCode model variants",
            )
        efforts: list[HarnessEffortInfo] = []
        for value in cast(dict[object, object], variants):
            if not isinstance(value, str) or not value:
                raise DomainError(
                    ErrorCode.PROVIDER_INCOMPATIBLE,
                    "malformed OpenCode model variant",
                )
            efforts.append(HarnessEffortInfo(id=value, label=value.title()))
        label = values.get("name")
        models.append(
            HarnessModelInfo(
                id=model_id,
                label=label if isinstance(label, str) and label else None,
                efforts=tuple(efforts),
            )
        )
    if not models:
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            "OpenCode advertised no models",
        )
    return tuple(models)

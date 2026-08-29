"""Grok version probe against the packaged compatibility source."""

from __future__ import annotations

import asyncio
import contextlib
import sys
from pathlib import Path
from typing import cast
from uuid import uuid4

from tth_types.enums import ErrorCode
from tth_types.errors import DomainError
from tth_types.harness import (
    HarnessCapabilities,
    HarnessConfiguration,
    HarnessModelInfo,
)

from tth_grok.acp.connection import AcpConnection
from tth_grok.acp.protocol import grok_acp_protocol
from tth_grok.harness.argv import build_grok_argv
from tth_grok.harness.compatibility import (
    GrokReleaseRecord,
    match_release,
)
from tth_grok.harness.control import initialize_grok
from tth_grok.runtime.spec import ProcessSpec
from tth_grok.runtime.supervisor import ProcessSupervisor
from tth_grok.shared.effort import validate_effort
from tth_grok.shared.model_discovery import run_model_command
from tth_grok.shared.paths import resolve_kind_executable


async def probe_grok(config: HarnessConfiguration) -> tuple[HarnessCapabilities, GrokReleaseRecord]:
    """Run ``grok --version``, match compatibility, return capabilities + release."""
    executable = resolve_kind_executable(config.kind)
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
    capabilities = release.to_harness_capabilities()
    load_session = await _probe_load_session(executable, config, release, capabilities)
    capabilities = capabilities.model_copy(
        update={"models": models, "supports_resume": load_session}
    )
    validate_effort(config, capabilities)
    return capabilities, release


async def _probe_load_session(
    executable: Path,
    config: HarnessConfiguration,
    release: GrokReleaseRecord,
    capabilities: HarnessCapabilities,
) -> bool:
    supervisor = ProcessSupervisor()
    launch = supervisor.build_launch_snapshot(
        executable_path=str(executable),
        working_directory=config.working_directory,
        workspace_roots=config.workspace_roots,
        capabilities=capabilities,
        model=None,
        mode=None,
        adapter_version="grok-capability-probe",
    )
    handle = await supervisor.spawn(
        ProcessSpec(
            conversation_id=uuid4(),
            binding_id=uuid4(),
            process_id=uuid4(),
            launch=launch,
            argv=build_grok_argv(),
        )
    )
    connection = AcpConnection(handle, protocol=grok_acp_protocol())

    async def ignore_session_update(_notification: object) -> None:
        return None

    connection.set_notification_handler("session/update", ignore_session_update)
    try:
        await connection.start()
        init_result = await initialize_grok(connection, release)
        return (
            isinstance(init_result.get("agentCapabilities"), dict)
            and cast(dict[object, object], init_result["agentCapabilities"]).get("loadSession")
            is True
        )
    finally:
        with contextlib.suppress(Exception):
            await connection.close()
        with contextlib.suppress(Exception):
            await handle.close()


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

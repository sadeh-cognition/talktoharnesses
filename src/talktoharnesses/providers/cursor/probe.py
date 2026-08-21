"""Cursor version probe against the packaged compatibility source."""

from __future__ import annotations

import asyncio
import contextlib
import sys
from pathlib import Path
from typing import cast
from uuid import uuid4

from talktoharnesses.domain.enums import ErrorCode
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.domain.models import (
    HarnessCapabilities,
    HarnessConfiguration,
    HarnessEffortInfo,
    HarnessModelInfo,
)
from talktoharnesses.providers._model_discovery import run_model_command
from talktoharnesses.providers.acp.connection import AcpConnection
from talktoharnesses.providers.acp.protocol import cursor_acp_protocol
from talktoharnesses.providers.acp.schemas.cursor_ext import (
    CursorSelectConfigOption,
    parse_cursor_config_options,
)
from talktoharnesses.providers.cursor.argv import build_cursor_argv
from talktoharnesses.providers.cursor.compatibility import (
    CursorReleaseRecord,
    match_release,
)
from talktoharnesses.providers.cursor.control import (
    find_cursor_config_option,
    initialize_cursor,
    set_cursor_config_option,
)
from talktoharnesses.providers.effort import validate_effort
from talktoharnesses.runtime.paths import resolve_kind_executable
from talktoharnesses.runtime.spec import ProcessSpec
from talktoharnesses.runtime.supervisor import ProcessSupervisor

_EFFORT_CACHE: dict[
    tuple[str, str, str, bool, str],
    tuple[tuple[HarnessEffortInfo, ...], tuple[HarnessModelInfo, ...], bool],
] = {}


async def probe_cursor(
    config: HarnessConfiguration,
) -> tuple[HarnessCapabilities, CursorReleaseRecord]:
    """Run ``agent --version``, match compatibility, return capabilities + release."""
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
            f"failed to execute cursor agent: {exc}",
            details={"executable": str(executable)},
        ) from exc
    stdout_b, stderr_b = await proc.communicate()
    if proc.returncode not in (0, None):
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            "cursor agent --version failed",
            details={
                "returncode": proc.returncode,
                "stderr": stderr_b.decode("utf-8", errors="replace")[:500],
            },
        )
    version_stdout = stdout_b.decode("utf-8", errors="replace")
    release = match_release(version_stdout, platform=sys.platform)
    output = await run_model_command(
        executable,
        "--list-models",
        provider="Cursor",
        working_directory=config.working_directory,
    )
    models = _parse_models(output)
    capabilities = release.to_harness_capabilities()
    cache_key = (
        str(executable),
        release.id,
        config.working_directory,
        config.yolo,
        output,
    )
    cached = _EFFORT_CACHE.get(cache_key)
    if cached is None:
        cached = await _discover_model_efforts(
            executable,
            config,
            release,
            capabilities,
            models,
        )
        _EFFORT_CACHE[cache_key] = cached
    default_efforts, models, load_session = cached
    capabilities = capabilities.model_copy(
        update={
            "models": models,
            "efforts": default_efforts,
            "supports_resume": load_session,
        }
    )
    validate_effort(config, capabilities)
    return capabilities, release


async def _discover_model_efforts(
    executable: Path,
    config: HarnessConfiguration,
    release: CursorReleaseRecord,
    capabilities: HarnessCapabilities,
    models: tuple[HarnessModelInfo, ...],
) -> tuple[tuple[HarnessEffortInfo, ...], tuple[HarnessModelInfo, ...], bool]:
    supervisor = ProcessSupervisor()
    launch = supervisor.build_launch_snapshot(
        executable_path=str(executable),
        working_directory=config.working_directory,
        workspace_roots=config.workspace_roots,
        capabilities=capabilities,
        model=None,
        mode=None,
        adapter_version="cursor-effort-probe",
    )
    handle = await supervisor.spawn(
        ProcessSpec(
            conversation_id=uuid4(),
            binding_id=uuid4(),
            process_id=uuid4(),
            launch=launch,
            argv=build_cursor_argv(),
        )
    )
    connection = AcpConnection(handle, protocol=cursor_acp_protocol())

    async def ignore_session_update(_notification: object) -> None:
        return None

    connection.set_notification_handler("session/update", ignore_session_update)
    try:
        await connection.start()
        init_result = await initialize_cursor(connection, release)
        load_session = (
            isinstance(init_result.get("agentCapabilities"), dict)
            and cast(dict[object, object], init_result["agentCapabilities"]).get("loadSession")
            is True
        )
        future, _ = await connection.request(
            "session/new",
            {"cwd": launch.working_directory, "mcpServers": []},
        )
        result = await future
        if not isinstance(result, dict):
            raise DomainError(
                ErrorCode.PROTOCOL_ERROR,
                "Cursor effort probe session result must be an object",
            )
        session_id_obj = cast(dict[object, object], result).get("sessionId")
        if not isinstance(session_id_obj, str) or not session_id_obj:
            raise DomainError(
                ErrorCode.PROTOCOL_ERROR,
                "Cursor effort probe session result missing sessionId",
            )
        session_id = session_id_obj
        options = parse_cursor_config_options(cast(object, result))
        default_efforts = _efforts_from_options(options)
        model_option = find_cursor_config_option(options, "model")
        if model_option is None:
            raise DomainError(
                ErrorCode.PROVIDER_INCOMPATIBLE,
                "Cursor effort probe did not advertise a model option",
            )
        listed_models = {model.id: model for model in models}
        selectable_models = tuple(
            listed_models.get(
                "auto" if item.value == "default" else item.value,
                HarnessModelInfo(
                    id="auto" if item.value == "default" else item.value,
                    label=item.name,
                ),
            )
            for item in model_option.options
        )
        discovered: list[HarnessModelInfo] = []
        for model in selectable_models:
            model_value = "default" if model.id == "auto" else model.id
            model_option = find_cursor_config_option(options, "model")
            if model_option is None:
                raise DomainError(
                    ErrorCode.PROVIDER_INCOMPATIBLE,
                    "Cursor effort probe did not advertise a model option",
                )
            if model_option.currentValue != model_value:
                options = await set_cursor_config_option(
                    connection,
                    session_id=session_id,
                    config_id="model",
                    value=model_value,
                    options=options,
                )
            discovered.append(model.model_copy(update={"efforts": _efforts_from_options(options)}))
        return default_efforts, tuple(discovered), load_session
    finally:
        with contextlib.suppress(Exception):
            await connection.close()
        with contextlib.suppress(Exception):
            await handle.close()


def _efforts_from_options(
    options: tuple[CursorSelectConfigOption, ...],
) -> tuple[HarnessEffortInfo, ...]:
    thought_options = tuple(
        option
        for option in options
        if option.category == "thought_level"
        and {item.value for item in option.options} != {"false", "true"}
    )
    if not thought_options:
        return ()
    if len(thought_options) != 1:
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            "Cursor model advertised multiple thought-level options",
            details={"advertised_count": len(thought_options)},
        )
    return tuple(
        HarnessEffortInfo(id=item.value, label=item.name) for item in thought_options[0].options
    )


def _parse_models(output: str) -> tuple[HarnessModelInfo, ...]:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines or lines[0] != "Available models":
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            "malformed Cursor model list",
        )
    models: list[HarnessModelInfo] = []
    for line in lines[1:]:
        if line.startswith("Tip: use --model "):
            continue
        model_id, separator, label = line.partition(" - ")
        if not separator or not model_id or not label:
            raise DomainError(
                ErrorCode.PROVIDER_INCOMPATIBLE,
                "malformed Cursor model list",
            )
        models.append(
            HarnessModelInfo(
                id=model_id,
                label=label.removesuffix(" (default)"),
            )
        )
    if not models:
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            "Cursor advertised no models",
        )
    return tuple(models)

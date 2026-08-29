"""Cursor version probe against the packaged compatibility source."""

from __future__ import annotations

import asyncio
import contextlib
import sys
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import cast
from uuid import uuid4

from tth_types.enums import ErrorCode
from tth_types.errors import DomainError
from tth_types.harness import (
    HarnessCapabilities,
    HarnessConfiguration,
    HarnessEffortInfo,
    HarnessModelInfo,
    LaunchSnapshot,
)

from tth_cursor.acp.connection import AcpConnection
from tth_cursor.acp.protocol import cursor_acp_protocol
from tth_cursor.acp.schemas.cursor_ext import (
    CursorSelectConfigOption,
    parse_cursor_config_options,
)
from tth_cursor.harness.argv import build_cursor_argv
from tth_cursor.harness.compatibility import (
    CursorReleaseRecord,
    match_release,
)
from tth_cursor.harness.control import (
    find_cursor_config_option,
    initialize_cursor,
    set_cursor_config_option,
)
from tth_cursor.runtime.handle import ProcessHandle
from tth_cursor.runtime.spec import ProcessSpec
from tth_cursor.runtime.supervisor import ProcessSupervisor
from tth_cursor.shared.effort import validate_effort
from tth_cursor.shared.model_discovery import run_model_command
from tth_cursor.shared.paths import resolve_kind_executable

_EFFORT_WORKER_COUNT = 4
_EffortDiscovery = tuple[
    tuple[HarnessEffortInfo, ...],
    tuple[HarnessModelInfo, ...],
    bool,
]
_EffortCacheKey = tuple[str, str, str, str]
_EFFORT_CACHE: dict[
    _EffortCacheKey,
    _EffortDiscovery,
] = {}
_EFFORT_IN_FLIGHT: dict[
    tuple[asyncio.AbstractEventLoop, _EffortCacheKey],
    asyncio.Task[_EffortDiscovery],
] = {}


@dataclass(slots=True)
class _EffortWorker:
    handle: ProcessHandle
    connection: AcpConnection
    session_id: str
    options: tuple[CursorSelectConfigOption, ...]
    load_session: bool


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
    cache_key = (str(executable), release.id, config.working_directory, output)
    cached = await _cached_model_efforts(
        cache_key,
        executable,
        config,
        release,
        capabilities,
        models,
    )
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


async def _cached_model_efforts(
    cache_key: _EffortCacheKey,
    executable: Path,
    config: HarnessConfiguration,
    release: CursorReleaseRecord,
    capabilities: HarnessCapabilities,
    models: tuple[HarnessModelInfo, ...],
) -> _EffortDiscovery:
    cached = _EFFORT_CACHE.get(cache_key)
    if cached is not None:
        return cached

    loop = asyncio.get_running_loop()
    in_flight_key = (loop, cache_key)
    task = _EFFORT_IN_FLIGHT.get(in_flight_key)
    if task is None:
        task = asyncio.create_task(
            _discover_model_efforts(
                executable,
                config,
                release,
                capabilities,
                models,
            ),
            name="cursor-effort-discovery",
        )
        _EFFORT_IN_FLIGHT[in_flight_key] = task
        task.add_done_callback(partial(_finish_effort_discovery, cache_key, in_flight_key))
    result = await asyncio.shield(task)
    _EFFORT_CACHE[cache_key] = result
    return result


def _finish_effort_discovery(
    cache_key: _EffortCacheKey,
    in_flight_key: tuple[asyncio.AbstractEventLoop, _EffortCacheKey],
    task: asyncio.Task[_EffortDiscovery],
) -> None:
    if _EFFORT_IN_FLIGHT.get(in_flight_key) is task:
        del _EFFORT_IN_FLIGHT[in_flight_key]
    if task.cancelled():
        return
    try:
        result = task.result()
    except Exception:  # noqa: BLE001 - failed discovery remains retryable
        return
    _EFFORT_CACHE[cache_key] = result


async def _discover_model_efforts(
    executable: Path,
    config: HarnessConfiguration,
    release: CursorReleaseRecord,
    capabilities: HarnessCapabilities,
    models: tuple[HarnessModelInfo, ...],
) -> _EffortDiscovery:
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
    opened: list[_EffortWorker] = []

    async def open_worker() -> _EffortWorker:
        worker = await _open_effort_worker(supervisor, launch, release)
        opened.append(worker)
        return worker

    open_tasks = [
        asyncio.create_task(open_worker(), name=f"cursor-effort-worker-{index}")
        for index in range(min(_EFFORT_WORKER_COUNT, len(models)))
    ]
    try:
        workers = await asyncio.gather(*open_tasks)
        model_options = tuple(_require_model_option(worker.options) for worker in workers)
        expected_catalog = _model_option_catalog(model_options[0])
        if any(_model_option_catalog(option) != expected_catalog for option in model_options[1:]):
            raise DomainError(
                ErrorCode.PROVIDER_INCOMPATIBLE,
                "Cursor effort probe workers advertised inconsistent model options",
            )

        default_efforts = _efforts_from_options(workers[0].options)
        listed_models = {model.id: model for model in models}
        selectable_models = tuple(
            listed_models.get(
                "auto" if item.value == "default" else item.value,
                HarnessModelInfo(
                    id="auto" if item.value == "default" else item.value,
                    label=item.name,
                ),
            )
            for item in model_options[0].options
        )
        indexed_models = tuple(enumerate(selectable_models))
        discovery_tasks = [
            asyncio.create_task(
                _discover_worker_models(worker, indexed_models[index :: len(workers)]),
                name=f"cursor-effort-models-{index}",
            )
            for index, worker in enumerate(workers)
        ]
        try:
            chunks = await asyncio.gather(*discovery_tasks)
        finally:
            for task in discovery_tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*discovery_tasks, return_exceptions=True)
        discovered = sorted(
            (item for chunk in chunks for item in chunk),
            key=lambda item: item[0],
        )
        return (
            default_efforts,
            tuple(model for _index, model in discovered),
            workers[0].load_session,
        )
    finally:
        for task in open_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*open_tasks, return_exceptions=True)
        await asyncio.gather(*(_close_effort_worker(worker) for worker in opened))


async def _open_effort_worker(
    supervisor: ProcessSupervisor,
    launch: LaunchSnapshot,
    release: CursorReleaseRecord,
) -> _EffortWorker:
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
    connection.set_notification_handler("session/update", _ignore_session_update)
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
        return _EffortWorker(
            handle=handle,
            connection=connection,
            session_id=session_id_obj,
            options=parse_cursor_config_options(cast(object, result)),
            load_session=load_session,
        )
    except BaseException:
        await _close_effort_worker(
            _EffortWorker(
                handle=handle,
                connection=connection,
                session_id="",
                options=(),
                load_session=False,
            )
        )
        raise


async def _ignore_session_update(_notification: object) -> None:
    return None


async def _close_effort_worker(worker: _EffortWorker) -> None:
    with contextlib.suppress(Exception):
        await worker.connection.close()
    with contextlib.suppress(Exception):
        await worker.handle.close()


def _require_model_option(
    options: tuple[CursorSelectConfigOption, ...],
) -> CursorSelectConfigOption:
    model_option = find_cursor_config_option(options, "model")
    if model_option is None:
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            "Cursor effort probe did not advertise a model option",
        )
    return model_option


def _model_option_catalog(
    model_option: CursorSelectConfigOption,
) -> tuple[tuple[str, str], ...]:
    return tuple((item.value, item.name) for item in model_option.options)


async def _discover_worker_models(
    worker: _EffortWorker,
    indexed_models: tuple[tuple[int, HarnessModelInfo], ...],
) -> tuple[tuple[int, HarnessModelInfo], ...]:
    options = worker.options
    discovered: list[tuple[int, HarnessModelInfo]] = []
    for index, model in indexed_models:
        model_value = "default" if model.id == "auto" else model.id
        model_option = _require_model_option(options)
        if model_option.currentValue != model_value:
            options = await set_cursor_config_option(
                worker.connection,
                session_id=worker.session_id,
                config_id="model",
                value=model_value,
                options=options,
            )
        discovered.append(
            (index, model.model_copy(update={"efforts": _efforts_from_options(options)}))
        )
    return tuple(discovered)


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

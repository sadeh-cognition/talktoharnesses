"""Probe the installed CLI and its live MSP model catalog."""

from __future__ import annotations

import asyncio
import os
import re
import sys
import time
from uuid import uuid4

from tth_types.enums import ErrorCode, HarnessKind
from tth_types.errors import DomainError
from tth_types.harness import HarnessCapabilities, HarnessConfiguration, HarnessModelInfo

from tth_muse.harness.compatibility import compare_versions, load_muse_compatibility
from tth_muse.harness.connection import MuseConnection
from tth_muse.runtime.spec import ProcessSpec
from tth_muse.runtime.supervisor import ProcessSupervisor
from tth_muse.shared.compatibility import assert_supported_platform, reject_below_floor
from tth_muse.shared.paths import resolve_kind_executable


def build_argv(config: HarnessConfiguration) -> tuple[str, ...]:
    return ("serve", "--disable-sandbox", "--trust-workspace") if config.yolo else ("serve",)


# A session start probes twice (``/v1/probe`` then ``/v1/sessions``), and each
# uncached probe spawns ``muse --version`` plus a throwaway ``muse serve`` for
# the model catalog. Remember the result briefly per executable build so a
# start spawns one host, not three processes.
_PROBE_CACHE_TTL_SECONDS = 60.0
_probe_cache: dict[tuple[str, int, int], tuple[float, str, tuple[HarnessModelInfo, ...]]] = {}


def reset_probe_cache_for_tests() -> None:
    _probe_cache.clear()


def _cache_key(executable: os.PathLike[str] | str) -> tuple[str, int, int]:
    stat = os.stat(executable)
    return (str(executable), stat.st_mtime_ns, stat.st_size)


async def probe_muse(config: HarnessConfiguration) -> HarnessCapabilities:
    if config.mode is not None or config.effort is not None:
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE, "Muse MSP does not advertise mode or effort discovery"
        )
    executable = resolve_kind_executable(HarnessKind.MUSE)
    key = _cache_key(executable)
    cached = _probe_cache.get(key)
    if cached is not None and time.monotonic() - cached[0] < _PROBE_CACHE_TTL_SECONDS:
        version, models = cached[1], cached[2]
    else:
        version, models = await _inspect_host(executable, config)
        _probe_cache[key] = (time.monotonic(), version, models)
    doc = load_muse_compatibility()
    if config.model is not None and config.model not in {model.id for model in models}:
        raise DomainError(ErrorCode.PROVIDER_INCOMPATIBLE, "Muse model is not in its catalog")
    return HarnessCapabilities(
        kind=HarnessKind.MUSE,
        version=version,
        models=models,
        **doc.floor.capabilities.model_dump(),
    )


async def _inspect_host(
    executable: os.PathLike[str] | str, config: HarnessConfiguration
) -> tuple[str, tuple[HarnessModelInfo, ...]]:
    """Run ``muse --version`` and read the live ``model/list`` from a host."""
    process = await asyncio.create_subprocess_exec(
        str(executable),
        "--version",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _stderr = await process.communicate()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    match = re.fullmatch(r"Muse Code (\d+\.\d+\.\d+) \(([^)]+)\)\s*", stdout.decode())
    if process.returncode != 0 or match is None:
        raise DomainError(ErrorCode.PROVIDER_INCOMPATIBLE, "Malformed Muse version output")
    version = match[2]
    doc = load_muse_compatibility()
    # Reject unsupported builds before spawning a host for the catalog.
    assert_supported_platform(sys.platform, doc.floor.platforms, harness_label="muse")
    reject_below_floor(
        probed=version,
        floor=doc.floor.version,
        compare=compare_versions,
        harness_label="muse",
        details={"cli_version": version},
    )
    caps = HarnessCapabilities(
        kind=HarnessKind.MUSE, version=version, **doc.floor.capabilities.model_dump()
    )
    supervisor = ProcessSupervisor()
    launch = supervisor.build_launch_snapshot(
        executable_path=str(executable),
        working_directory=config.working_directory,
        workspace_roots=config.workspace_roots,
        capabilities=caps,
        model=config.model,
        mode=config.mode,
        adapter_version=doc.adapter_version,
    )
    handle = await supervisor.spawn(
        ProcessSpec(
            conversation_id=uuid4(),
            binding_id=uuid4(),
            process_id=uuid4(),
            launch=launch,
            argv=build_argv(config),
        )
    )

    async def ignore(*_args: object) -> None:
        pass

    connection = MuseConnection(handle, ignore, ignore)
    try:
        await connection.initialize()
        catalog = await connection.request("model/list")
        return version, tuple(
            HarnessModelInfo(id=row["modelId"], label=row.get("displayLabel") or row["modelId"])
            for row in catalog["models"]
        )
    finally:
        await connection.close()
        await handle.close()

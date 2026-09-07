"""Muse split orchestration using the established supervised session lifecycle."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Awaitable, Callable
from importlib import import_module
from typing import cast
from uuid import UUID, uuid4

from tth_types.adapter import (
    HarnessAdapter,
    HarnessSession,
    ResumeSessionRequest,
    StartSessionRequest,
)
from tth_types.enums import ErrorCode, HarnessKind
from tth_types.errors import DomainError
from tth_types.harness import (
    HarnessCapabilities,
    HarnessConfiguration,
    LaunchSnapshot,
    VersionAdvisory,
)
from tth_types.mcp import require_mcp_servers_supported
from tth_types.split_api import CreateSessionRequest, ProbeRequest, ProbeResponse, SessionCreated

from tth_muse.harness.adapter import MuseAdapter
from tth_muse.harness.compatibility import compare_versions, load_muse_compatibility
from tth_muse.harness.wire_log import WireLog
from tth_muse.runtime.handle import ProcessHandle
from tth_muse.runtime.spec import ProcessSpec
from tth_muse.runtime.supervisor import ProcessSupervisor
from tth_muse.sessions import SessionEntry, get_session_store
from tth_muse.shared.compatibility import (
    comparable_probe_version,
    version_advisory,
)
from tth_muse.shared.paths import resolve_directory, resolve_kind_executable
from tth_muse.shared.policy import RuntimePolicy

logger = logging.getLogger(__name__)

KIND = HarnessKind.MUSE

_policy = RuntimePolicy()
_supervisor = ProcessSupervisor(_policy)

AdapterFactory = Callable[[], HarnessAdapter]
RetryStartup = Callable[[DomainError], Awaitable[tuple[str, ...] | None]]


def _default_adapter_factory() -> HarnessAdapter:
    return MuseAdapter(
        push_stall_probe=_policy.push_stall_probe,
        push_poll_interval=_policy.push_poll_interval,
        push_recovery_page_limit=_policy.push_recovery_page_limit,
        push_recovery_max_pages=_policy.push_recovery_max_pages,
    )


def _resolve_adapter_factory() -> AdapterFactory:
    """Resolve the adapter factory, honouring the test/dev override.

    ``TTH_SPLIT_ADAPTER_FACTORY=module.path:attribute`` substitutes a fake
    adapter factory so the service can run end-to-end without the real CLI.
    """
    spec = os.environ.get("TTH_SPLIT_ADAPTER_FACTORY")
    if not spec:
        return _default_adapter_factory
    module_name, _, attribute = spec.partition(":")
    module = import_module(module_name)
    return cast(AdapterFactory, getattr(module, attribute))


adapter_factory: AdapterFactory = _resolve_adapter_factory()


def _apply_redaction(adapter: HarnessAdapter, patterns: tuple[str, ...]) -> None:
    hook = getattr(adapter, "set_redaction_patterns", None)
    if callable(hook):
        cast(Callable[[tuple[str, ...]], None], hook)(patterns)


def _import_seen(
    adapter: HarnessAdapter,
    native_ids: tuple[str, ...],
    stream_offsets: tuple[str, ...],
) -> None:
    hook = getattr(adapter, "import_seen", None)
    if callable(hook):
        cast(Callable[[frozenset[str], frozenset[str]], None], hook)(
            frozenset(native_ids), frozenset(stream_offsets)
        )


def _preflight(adapter: HarnessAdapter, mode: str) -> None:
    hook = getattr(adapter, "preflight_operation", None)
    if callable(hook):
        cast(Callable[[str], None], hook)(mode)


def _build_argv(
    adapter: HarnessAdapter, configuration: HarnessConfiguration
) -> tuple[str, ...] | None:
    """Argv for process-bound adapters; None marks an SDK-style (no-spawn) adapter."""
    build = getattr(adapter, "build_argv", None)
    if not callable(build):
        return None
    built = cast(Callable[[HarnessConfiguration], object], build)(configuration)
    if isinstance(built, tuple | list):
        return tuple(str(part) for part in cast(tuple[object, ...] | list[object], built))
    raise DomainError(ErrorCode.INVALID_STATE, "build_argv must return a sequence of strings")


def _build_environment(
    adapter: HarnessAdapter, configuration: HarnessConfiguration
) -> dict[str, str]:
    """Extra child environment for process-bound adapters; empty when unsupported."""
    build = getattr(adapter, "build_environment", None)
    if not callable(build):
        return {}
    built = cast(Callable[[HarnessConfiguration], object], build)(configuration)
    if built is None:
        return {}
    if isinstance(built, dict):
        return {str(key): str(value) for key, value in cast(dict[object, object], built).items()}
    raise DomainError(ErrorCode.INVALID_STATE, "build_environment must return a mapping")


def _attach_wire_log(adapter: HarnessAdapter, session_id: UUID) -> None:
    attach = getattr(adapter, "attach_wire_log", None)
    if callable(attach):
        cast(Callable[[WireLog | None], None], attach)(WireLog.open(session_id))


def _bind_process(adapter: HarnessAdapter, handle: ProcessHandle) -> None:
    bind = getattr(adapter, "bind_process", None)
    if callable(bind):
        cast(Callable[[ProcessHandle], None], bind)(handle)


def _build_launch(
    configuration: HarnessConfiguration,
    capabilities: HarnessCapabilities,
    adapter_version: str,
    *,
    process_bound: bool,
) -> LaunchSnapshot:
    if process_bound:
        return _supervisor.build_launch_snapshot(
            executable_path=str(resolve_kind_executable(KIND)),
            working_directory=configuration.working_directory,
            workspace_roots=configuration.workspace_roots,
            capabilities=capabilities,
            model=configuration.model,
            mode=configuration.mode,
            adapter_version=adapter_version,
            effort=configuration.effort,
        )
    workdir = resolve_directory(
        configuration.working_directory,
        error_code=ErrorCode.WORKING_DIRECTORY_NOT_FOUND,
    )
    roots = tuple(
        str(resolve_directory(root, error_code=ErrorCode.WORKSPACE_ROOT_NOT_FOUND))
        for root in configuration.workspace_roots
    )
    return LaunchSnapshot(
        resolved_executable=None,
        harness_version=capabilities.version,
        working_directory=str(workdir),
        workspace_roots=roots,
        model=configuration.model,
        mode=configuration.mode,
        effort=configuration.effort,
        adapter_version=adapter_version,
        capabilities=capabilities,
    )


def _advisory(capabilities: HarnessCapabilities) -> VersionAdvisory:
    doc = load_muse_compatibility()
    latest = doc.latest_verified.version if doc.latest_verified is not None else None
    return version_advisory(
        probed=comparable_probe_version(capabilities),
        floor=doc.floor.version,
        latest_verified=latest,
        compare=compare_versions,
    )


async def probe(request: ProbeRequest) -> ProbeResponse:
    adapter = adapter_factory()
    _apply_redaction(adapter, request.redaction_patterns)
    capabilities = await asyncio.wait_for(
        adapter.probe(request.configuration),
        timeout=_policy.start_resume_timeout,
    )
    require_mcp_servers_supported(request.configuration, capabilities)
    process_bound = callable(getattr(adapter, "build_argv", None))
    launch = _build_launch(
        request.configuration,
        capabilities,
        request.adapter_version,
        process_bound=process_bound,
    )
    return ProbeResponse(
        capabilities=capabilities,
        launch=launch,
        advisory=_advisory(capabilities),
    )


async def _spawn(
    *,
    conversation_id: UUID,
    binding_id: UUID,
    launch: LaunchSnapshot,
    argv: tuple[str, ...],
    redaction_patterns: tuple[str, ...],
    environment: dict[str, str] | None = None,
) -> ProcessHandle:
    return await _supervisor.spawn(
        ProcessSpec(
            conversation_id=conversation_id,
            binding_id=binding_id,
            process_id=uuid4(),
            launch=launch,
            argv=argv,
            environment=environment or {},
        ),
        redaction_patterns=redaction_patterns,
    )


async def _start_or_resume(
    adapter: HarnessAdapter,
    request: CreateSessionRequest,
    launch: LaunchSnapshot,
) -> HarnessSession:
    if request.mode == "start":
        return await asyncio.wait_for(
            adapter.start(
                StartSessionRequest(
                    conversation_id=request.conversation_id,
                    binding_id=request.binding_id,
                    configuration=request.configuration,
                    launch=launch,
                )
            ),
            timeout=_policy.start_resume_timeout,
        )
    if request.native_session_id is None:
        raise DomainError(ErrorCode.INVALID_STATE, "resume requires native_session_id")
    return await asyncio.wait_for(
        adapter.resume(
            ResumeSessionRequest(
                conversation_id=request.conversation_id,
                binding_id=request.binding_id,
                configuration=request.configuration,
                native_session_id=request.native_session_id,
                launch=launch,
            )
        ),
        timeout=_policy.start_resume_timeout,
    )


async def create_session(request: CreateSessionRequest) -> SessionCreated:
    adapter = adapter_factory()
    _attach_wire_log(adapter, request.session_id)
    _apply_redaction(adapter, request.redaction_patterns)
    logger.info(
        "create_session %s mode=%s conversation=%s binding=%s native_session_id=%s",
        request.session_id,
        request.mode,
        request.conversation_id,
        request.binding_id,
        request.native_session_id,
    )
    _import_seen(adapter, request.seen_native_ids, request.seen_stream_offsets)

    capabilities = await asyncio.wait_for(
        adapter.probe(request.configuration),
        timeout=_policy.start_resume_timeout,
    )
    require_mcp_servers_supported(request.configuration, capabilities)
    argv = _build_argv(adapter, request.configuration)
    environment = _build_environment(adapter, request.configuration) if argv is not None else {}
    launch = _build_launch(
        request.configuration,
        capabilities,
        request.adapter_version,
        process_bound=argv is not None,
    )
    _preflight(adapter, request.mode)
    store = get_session_store()
    await store.close_binding(request.conversation_id, request.binding_id)

    handle: ProcessHandle | None = None
    try:
        if argv is not None:
            handle = await _spawn(
                conversation_id=request.conversation_id,
                binding_id=request.binding_id,
                launch=launch,
                argv=argv,
                redaction_patterns=request.redaction_patterns,
                environment=environment,
            )
            _bind_process(adapter, handle)

        startup_retried = False
        while True:
            try:
                session = await _start_or_resume(adapter, request, launch)
                break
            except DomainError as exc:
                retry_obj = getattr(adapter, "retry_startup", None)
                if startup_retried or not callable(retry_obj) or handle is None:
                    raise
                retry_argv = await cast(RetryStartup, retry_obj)(exc)
                if retry_argv is None:
                    raise
                startup_retried = True
                await handle.force_terminate(reason="startup_bind_retry")
                handle = await _spawn(
                    conversation_id=request.conversation_id,
                    binding_id=request.binding_id,
                    launch=launch,
                    argv=tuple(str(part) for part in retry_argv),
                    redaction_patterns=request.redaction_patterns,
                    environment=environment,
                )
                _bind_process(adapter, handle)
        # Registered inside the cleanup scope: a capacity refusal from the
        # store must terminate the process and close the just-started session
        # instead of orphaning them.
        entry: SessionEntry = store.add(
            adapter,
            session,
            launch,
            handle=handle,
            session_id=request.session_id,
        )
    except BaseException:
        if handle is not None:
            with contextlib.suppress(Exception):
                await handle.force_terminate(reason="startup_failure")
        provisional = HarnessSession(
            conversation_id=request.conversation_id,
            binding_id=request.binding_id,
            kind=request.configuration.kind,
            model=request.configuration.model,
            mode=request.configuration.mode,
            effort=request.configuration.effort,
        )
        with contextlib.suppress(Exception):
            await asyncio.wait_for(
                adapter.close(provisional),
                timeout=_policy.graceful_close_timeout,
            )
        raise

    session = session.model_copy(
        update={
            "metadata": {**session.metadata, "split_session_id": str(entry.session_id)},
        }
    )
    entry.session = session
    return SessionCreated(
        session_id=entry.session_id,
        session=session,
        launch=launch,
        pid=handle.pid if handle is not None else None,
    )

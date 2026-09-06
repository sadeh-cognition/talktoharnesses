"""Create-session and probe orchestration for the Claude split.

Absorbs the launch responsibilities the proxy's RuntimeManager used to hold
for this kind: probe with timeout, launch snapshot resolution, preflight, and
adapter start/resume. codex is SDK-managed, so there is no supervised process.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Callable
from importlib import import_module
from typing import cast

from tth_types.adapter import (
    HarnessAdapter,
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

from tth_codex.harness.adapter import CodexAdapter
from tth_codex.harness.compatibility import load_codex_compatibility
from tth_codex.sessions import SessionEntry, get_session_store
from tth_codex.shared.compatibility import (
    comparable_probe_version,
    compare_dotted,
    version_advisory,
)
from tth_codex.shared.paths import resolve_directory
from tth_codex.shared.policy import RuntimePolicy

KIND = HarnessKind.CODEX

_policy = RuntimePolicy()

AdapterFactory = Callable[[], HarnessAdapter]


def _default_adapter_factory() -> HarnessAdapter:
    return CodexAdapter()


def _resolve_adapter_factory() -> AdapterFactory:
    """Resolve the adapter factory, honouring the test/dev override.

    ``TTH_SPLIT_ADAPTER_FACTORY=module.path:attribute`` substitutes a fake
    adapter factory so the service can run end-to-end without the real SDK.
    """
    spec = os.environ.get("TTH_SPLIT_ADAPTER_FACTORY")
    if not spec:
        return _default_adapter_factory
    module_name, _, attribute = spec.partition(":")
    module = import_module(module_name)
    return cast(AdapterFactory, getattr(module, attribute))


# Overridable in tests to substitute a fake adapter.
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


def _build_launch(
    configuration: HarnessConfiguration,
    capabilities: HarnessCapabilities,
    adapter_version: str,
) -> LaunchSnapshot:
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
    doc = load_codex_compatibility()
    latest = doc.latest_verified.version if doc.latest_verified is not None else None
    return version_advisory(
        probed=comparable_probe_version(capabilities),
        floor=doc.floor.version,
        latest_verified=latest,
        compare=compare_dotted,
    )


async def probe(request: ProbeRequest) -> ProbeResponse:
    adapter = adapter_factory()
    _apply_redaction(adapter, request.redaction_patterns)
    capabilities = await asyncio.wait_for(
        adapter.probe(request.configuration),
        timeout=_policy.start_resume_timeout,
    )
    require_mcp_servers_supported(request.configuration, capabilities)
    launch = _build_launch(request.configuration, capabilities, request.adapter_version)
    return ProbeResponse(
        capabilities=capabilities,
        launch=launch,
        advisory=_advisory(capabilities),
    )


async def create_session(request: CreateSessionRequest) -> SessionCreated:
    adapter = adapter_factory()
    _apply_redaction(adapter, request.redaction_patterns)
    _import_seen(adapter, request.seen_native_ids, request.seen_stream_offsets)

    capabilities = await asyncio.wait_for(
        adapter.probe(request.configuration),
        timeout=_policy.start_resume_timeout,
    )
    require_mcp_servers_supported(request.configuration, capabilities)
    launch = _build_launch(request.configuration, capabilities, request.adapter_version)
    _preflight(adapter, request.mode)
    store = get_session_store()
    await store.close_binding(request.conversation_id, request.binding_id)

    if request.mode == "start":
        session = await asyncio.wait_for(
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
    else:
        if request.native_session_id is None:
            raise DomainError(
                ErrorCode.INVALID_STATE,
                "resume requires native_session_id",
            )
        session = await asyncio.wait_for(
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

    try:
        entry: SessionEntry = store.add(adapter, session, launch, session_id=request.session_id)
    except DomainError:
        # A capacity refusal here must not orphan the native session that
        # start/resume just created.
        with contextlib.suppress(Exception):
            await asyncio.wait_for(
                adapter.close(session),
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
        pid=None,
    )

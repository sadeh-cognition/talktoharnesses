"""RuntimeManager — one supervised runtime per conversation."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

from talktoharnesses.application.persistence import Persistence
from talktoharnesses.domain.enums import ErrorCode, ProcessStatus
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.domain.events import (
    ConversationEvent,
    EventPayload,
    ProcessExitedPayload,
    ProcessForcedTerminationPayload,
    ProcessStderrTruncatedPayload,
    ProviderWarningPayload,
)
from talktoharnesses.domain.models import (
    HarnessCapabilities,
    HarnessConfiguration,
    LaunchSnapshot,
    ProcessRecord,
)
from talktoharnesses.domain.transitions import (
    ConversationState,
    append_events,
    close_session,
    fail_session,
    reap_session,
    resume_session,
    start_session,
)
from talktoharnesses.providers._sdk_managed import SdkManagedAdapter
from talktoharnesses.providers.adapter import (
    HarnessAdapter,
    HarnessSession,
    ResumeSessionRequest,
    StartSessionRequest,
)
from talktoharnesses.providers.registry import AdapterRegistry
from talktoharnesses.runtime.events import (
    ProcessEvent,
    ProcessExitedEvent,
    ProcessForcedTerminationEvent,
    ProcessSilenceWarningEvent,
    ProcessStderrTruncatedEvent,
)
from talktoharnesses.runtime.handle import ProcessHandle
from talktoharnesses.runtime.paths import resolve_directory, resolve_executable
from talktoharnesses.runtime.policy import RuntimePolicy
from talktoharnesses.runtime.spec import ProcessSpec
from talktoharnesses.runtime.supervisor import ProcessSupervisor

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _empty_tasks() -> list[asyncio.Task[None]]:
    return []


def _is_sdk_managed(adapter: HarnessAdapter) -> bool:
    return isinstance(adapter, SdkManagedAdapter) or getattr(adapter, "sdk_managed", False) is True


@dataclass
class ManagedRuntime:
    conversation_id: UUID
    owner_id: str
    adapter: HarnessAdapter
    session: HarnessSession
    process: ProcessHandle | None
    process_record: ProcessRecord
    launch: LaunchSnapshot
    tasks: list[asyncio.Task[None]] = field(default_factory=_empty_tasks)
    closed: bool = False
    terminal_persisted: bool = False
    stderr_truncation_persisted: bool = False


class RuntimeManager:
    """Lifecycle-only runtime management: start/resume/close/reap/interrupt/shutdown.

    Command delivery and provider-event normalization are Phase 4 work.
    """

    def __init__(
        self,
        persistence: Persistence,
        registry: AdapterRegistry,
        *,
        policy: RuntimePolicy | None = None,
        supervisor: ProcessSupervisor | None = None,
        clock: Callable[[], datetime] | None = None,
        redaction_patterns: tuple[str, ...] = (),
    ) -> None:
        self._persistence = persistence
        self._registry = registry
        self._policy = policy or RuntimePolicy()
        self._supervisor = supervisor or ProcessSupervisor(
            self._policy,
            redaction_patterns=redaction_patterns,
        )
        self._clock = clock or _utc_now
        self._redaction_patterns = redaction_patterns

        self._runtimes: dict[UUID, ManagedRuntime] = {}
        self._locks: dict[UUID, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()
        self._shutting_down = False
        self._idle_tasks: dict[UUID, asyncio.Task[None]] = {}
        self._startup_tasks: set[asyncio.Task[object]] = set()

    def _lock_for(self, conversation_id: UUID) -> asyncio.Lock:
        lock = self._locks.get(conversation_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[conversation_id] = lock
        return lock

    def get_runtime(self, conversation_id: UUID) -> ManagedRuntime | None:
        return self._runtimes.get(conversation_id)

    async def start(
        self,
        *,
        conversation_id: UUID,
        owner_id: str,
        configuration: HarnessConfiguration,
        argv: tuple[str, ...],
        adapter_version: str = "0",
        executable_path: str | None = None,
    ) -> HarnessSession:
        """Create adapter, spawn process, start session, persist lifecycle."""
        return await self._start_request(
            conversation_id=conversation_id,
            owner_id=owner_id,
            configuration=configuration,
            argv=argv,
            adapter_version=adapter_version,
            executable_path=executable_path,
            resume_native_id=None,
        )

    async def resume(
        self,
        *,
        conversation_id: UUID,
        owner_id: str,
        configuration: HarnessConfiguration,
        native_session_id: str,
        argv: tuple[str, ...],
        adapter_version: str = "0",
        executable_path: str | None = None,
    ) -> HarnessSession:
        return await self._start_request(
            conversation_id=conversation_id,
            owner_id=owner_id,
            configuration=configuration,
            argv=argv,
            adapter_version=adapter_version,
            executable_path=executable_path,
            resume_native_id=native_session_id,
        )

    async def _start_request(
        self,
        *,
        conversation_id: UUID,
        owner_id: str,
        configuration: HarnessConfiguration,
        argv: tuple[str, ...],
        adapter_version: str,
        executable_path: str | None,
        resume_native_id: str | None,
    ) -> HarnessSession:
        task = asyncio.current_task()
        assert task is not None
        async with self._global_lock:
            if self._shutting_down:
                raise DomainError(ErrorCode.INVALID_STATE, "runtime manager is shutting down")
            self._startup_tasks.add(task)
        try:
            async with self._lock_for(conversation_id):
                if self._shutting_down:
                    raise DomainError(ErrorCode.INVALID_STATE, "runtime manager is shutting down")
                if conversation_id in self._runtimes:
                    raise DomainError(
                        ErrorCode.CONVERSATION_BUSY,
                        "conversation already has an active runtime",
                        details={"conversation_id": str(conversation_id)},
                    )
                return await self._start_or_resume(
                    conversation_id=conversation_id,
                    owner_id=owner_id,
                    configuration=configuration,
                    argv=argv,
                    adapter_version=adapter_version,
                    executable_path=executable_path,
                    resume_native_id=resume_native_id,
                )
        finally:
            async with self._global_lock:
                self._startup_tasks.discard(task)

    async def _start_or_resume(
        self,
        *,
        conversation_id: UUID,
        owner_id: str,
        configuration: HarnessConfiguration,
        argv: tuple[str, ...],
        adapter_version: str,
        executable_path: str | None,
        resume_native_id: str | None,
    ) -> HarnessSession:
        state = await self._persistence.get_snapshot(conversation_id, owner_id)
        if state.binding is None:
            raise DomainError(ErrorCode.INVALID_STATE, "conversation has no binding")

        binding = state.binding
        adapter = self._registry.create(configuration.kind)
        set_redaction_patterns = getattr(adapter, "set_redaction_patterns", None)
        if callable(set_redaction_patterns):
            set_redaction_patterns(self._redaction_patterns)
        sdk_managed = _is_sdk_managed(adapter)
        exe = executable_path or configuration.executable_path
        if not sdk_managed and not exe:
            raise DomainError(
                ErrorCode.INVALID_EXECUTABLE,
                "configuration has no executable_path",
            )

        # Process-bound adapters may construct argv when the caller passes empty.
        effective_argv: tuple[str, ...] = argv
        build_argv = getattr(adapter, "build_argv", None)
        if callable(build_argv) and not effective_argv:
            built_obj = build_argv(configuration)
            if isinstance(built_obj, tuple):
                effective_argv = tuple(str(part) for part in cast(tuple[object, ...], built_obj))
            elif isinstance(built_obj, list):
                effective_argv = tuple(str(part) for part in cast(list[object], built_obj))
            else:
                raise DomainError(
                    ErrorCode.INVALID_STATE,
                    "build_argv must return a sequence of strings",
                )

        process_id = uuid4()
        process_record = ProcessRecord(
            id=process_id,
            conversation_id=conversation_id,
            binding_id=binding.id,
            status=ProcessStatus.STARTING,
        )
        # Persist STARTING before spawn so no unrecorded process survives a crash
        # after successful spawn but before RUNNING is committed.
        try:
            await self._commit_process(
                state=state,
                process=process_record,
                launch_history_entry=None,
                events=(),
            )
            state = await self._persistence.get_snapshot(conversation_id, owner_id)
        except DomainError:
            raise

        handle: ProcessHandle | None = None
        launch: LaunchSnapshot | None = None
        try:
            caps = await asyncio.wait_for(
                adapter.probe(configuration),
                timeout=self._policy.start_resume_timeout,
            )
            if sdk_managed:
                launch = self._build_sdk_launch_snapshot(
                    executable_path=exe,
                    working_directory=configuration.working_directory,
                    workspace_roots=configuration.workspace_roots,
                    capabilities=caps,
                    model=configuration.model,
                    mode=configuration.mode,
                    adapter_version=adapter_version,
                )
            else:
                assert exe is not None
                launch = self._supervisor.build_launch_snapshot(
                    executable_path=exe,
                    working_directory=configuration.working_directory,
                    workspace_roots=configuration.workspace_roots,
                    capabilities=caps,
                    model=configuration.model,
                    mode=configuration.mode,
                    adapter_version=adapter_version,
                )
                spec = ProcessSpec(
                    conversation_id=conversation_id,
                    binding_id=binding.id,
                    process_id=process_id,
                    launch=launch,
                    argv=effective_argv,
                )

                handle = await self._supervisor.spawn(
                    spec,
                    redaction_patterns=self._redaction_patterns,
                )

                bind_process = getattr(adapter, "bind_process", None)
                if callable(bind_process):
                    bind_process(handle)

            process_record = process_record.model_copy(
                update={
                    "status": ProcessStatus.RUNNING,
                    "pid": handle.pid if handle is not None else None,
                    "started_at": self._clock(),
                }
            )
            assert state.binding is not None
            state = state.model_copy(
                update={"binding": state.binding.model_copy(update={"launch_snapshot": launch})}
            )
            await self._commit_process(
                state=state,
                process=process_record,
                launch_history_entry=launch,
                events=(),
            )
            state = await self._persistence.get_snapshot(conversation_id, owner_id)

            startup_retried = False
            while True:
                try:
                    if resume_native_id is None:
                        operation = adapter.start(
                            StartSessionRequest(
                                conversation_id=conversation_id,
                                binding_id=binding.id,
                                configuration=configuration,
                                launch=launch,
                            )
                        )
                    else:
                        operation = adapter.resume(
                            ResumeSessionRequest(
                                conversation_id=conversation_id,
                                binding_id=binding.id,
                                configuration=configuration,
                                native_session_id=resume_native_id,
                                launch=launch,
                            )
                        )
                    session = await asyncio.wait_for(
                        operation,
                        timeout=self._policy.start_resume_timeout,
                    )
                    break
                except DomainError as exc:
                    retry_startup_obj = getattr(adapter, "retry_startup", None)
                    if startup_retried or not callable(retry_startup_obj) or handle is None:
                        raise
                    retry_startup = cast(
                        Callable[[DomainError], Awaitable[tuple[str, ...] | None]],
                        retry_startup_obj,
                    )
                    retry_argv_obj = await retry_startup(exc)
                    if retry_argv_obj is None:
                        raise
                    startup_retried = True
                    await handle.force_terminate(reason="startup_bind_retry")
                    failed_record = process_record.model_copy(
                        update={
                            "status": ProcessStatus.FAILED,
                            "exited_at": self._clock(),
                            "exit_code": handle.returncode,
                            "redacted_stderr_tail": handle.redacted_stderr_tail,
                        }
                    )
                    await self._commit_process(
                        state=state,
                        process=failed_record,
                        launch_history_entry=None,
                        events=(),
                    )
                    state = await self._persistence.get_snapshot(conversation_id, owner_id)
                    process_id = uuid4()
                    process_record = ProcessRecord(
                        id=process_id,
                        conversation_id=conversation_id,
                        binding_id=binding.id,
                        status=ProcessStatus.STARTING,
                    )
                    await self._commit_process(
                        state=state,
                        process=process_record,
                        launch_history_entry=None,
                        events=(),
                    )
                    state = await self._persistence.get_snapshot(conversation_id, owner_id)
                    retry_argv = tuple(str(part) for part in retry_argv_obj)
                    handle = None
                    handle = await self._supervisor.spawn(
                        ProcessSpec(
                            conversation_id=conversation_id,
                            binding_id=binding.id,
                            process_id=process_id,
                            launch=launch,
                            argv=retry_argv,
                        ),
                        redaction_patterns=self._redaction_patterns,
                    )
                    bind_process = getattr(adapter, "bind_process", None)
                    if callable(bind_process):
                        bind_process(handle)
                    process_record = process_record.model_copy(
                        update={
                            "status": ProcessStatus.RUNNING,
                            "pid": handle.pid,
                            "started_at": self._clock(),
                        }
                    )
                    await self._commit_process(
                        state=state,
                        process=process_record,
                        launch_history_entry=None,
                        events=(),
                    )
                    state = await self._persistence.get_snapshot(conversation_id, owner_id)

            if resume_native_id is None:
                result = start_session(
                    state,
                    now=self._clock(),
                    native_session_id=session.native_session_id,
                    launch=launch,
                )
            else:
                result = resume_session(
                    state,
                    now=self._clock(),
                    native_session_id=session.native_session_id or resume_native_id,
                    launch=launch,
                )

            await self._persistence.commit_runtime_lifecycle(
                conversation_id,
                state.conversation.version,
                result.state,
                process_record,
                None,
                result.events,
            )
            state = await self._persistence.get_snapshot(conversation_id, owner_id)

            managed = ManagedRuntime(
                conversation_id=conversation_id,
                owner_id=owner_id,
                adapter=adapter,
                session=session,
                process=handle,
                process_record=process_record,
                launch=launch,
            )
            if handle is not None:
                pump = asyncio.create_task(
                    self._lifecycle_pump(managed),
                    name=f"lifecycle-{conversation_id}",
                )
                managed.tasks.append(pump)
            self._runtimes[conversation_id] = managed
            self._arm_idle_timer(conversation_id)
            return session

        except asyncio.CancelledError:
            if sdk_managed:
                await asyncio.shield(
                    self._rollback_sdk_startup(
                        adapter,
                        conversation_id=conversation_id,
                        binding_id=binding.id,
                        configuration=configuration,
                    )
                )
            if handle is not None:
                await asyncio.shield(handle.force_terminate(reason="startup_cancelled"))
                await asyncio.shield(
                    self._persist_failure(
                        conversation_id,
                        owner_id,
                        process_record,
                        handle,
                        ErrorCode.RUNTIME_TIMEOUT.value,
                        "session startup cancelled during shutdown",
                    )
                )
            elif sdk_managed:
                await asyncio.shield(
                    self._persist_failure(
                        conversation_id,
                        owner_id,
                        process_record,
                        None,
                        ErrorCode.RUNTIME_TIMEOUT.value,
                        "session startup cancelled during shutdown",
                    )
                )
            raise
        except TimeoutError as exc:
            if sdk_managed:
                await self._rollback_sdk_startup(
                    adapter,
                    conversation_id=conversation_id,
                    binding_id=binding.id,
                    configuration=configuration,
                )
            if handle is not None:
                await handle.force_terminate(reason="start_resume_timeout")
            await self._persist_failure(
                conversation_id,
                owner_id,
                process_record,
                handle,
                ErrorCode.RUNTIME_TIMEOUT.value,
                "session start/resume timed out",
            )
            raise DomainError(
                ErrorCode.RUNTIME_TIMEOUT,
                "session start/resume timed out",
                details={"conversation_id": str(conversation_id)},
            ) from exc
        except DomainError as exc:
            if sdk_managed:
                await self._rollback_sdk_startup(
                    adapter,
                    conversation_id=conversation_id,
                    binding_id=binding.id,
                    configuration=configuration,
                )
            if handle is not None:
                await handle.force_terminate(reason="startup_failure")
            await self._persist_failure(
                conversation_id,
                owner_id,
                process_record,
                handle,
                exc.code.value,
                exc.message,
            )
            raise
        except Exception as exc:
            if sdk_managed:
                await self._rollback_sdk_startup(
                    adapter,
                    conversation_id=conversation_id,
                    binding_id=binding.id,
                    configuration=configuration,
                )
            if handle is not None:
                await handle.force_terminate(reason="startup_failure")
            await self._persist_failure(
                conversation_id,
                owner_id,
                process_record,
                handle,
                ErrorCode.INVALID_STATE.value,
                str(exc),
            )
            raise

    async def _rollback_sdk_startup(
        self,
        adapter: HarnessAdapter,
        *,
        conversation_id: UUID,
        binding_id: UUID,
        configuration: HarnessConfiguration,
    ) -> None:
        provisional = HarnessSession(
            conversation_id=conversation_id,
            binding_id=binding_id,
            kind=configuration.kind,
            model=configuration.model,
            mode=configuration.mode,
        )
        with contextlib.suppress(Exception):
            await asyncio.wait_for(
                adapter.close(provisional),
                timeout=self._policy.graceful_close_timeout,
            )

    async def _persist_failure(
        self,
        conversation_id: UUID,
        owner_id: str,
        process_record: ProcessRecord,
        handle: ProcessHandle | None,
        error_code: str,
        message: str,
    ) -> None:
        while True:
            try:
                state = await self._persistence.get_snapshot(conversation_id, owner_id)
                now = self._clock()
                record = process_record.model_copy(
                    update={
                        "status": ProcessStatus.FAILED,
                        "exited_at": now,
                        "exit_code": handle.returncode if handle else None,
                        "redacted_stderr_tail": (handle.redacted_stderr_tail if handle else ""),
                    }
                )
                result = fail_session(
                    state,
                    now=now,
                    error_code=error_code,
                    message=message,
                )
                await self._persistence.commit_runtime_lifecycle(
                    conversation_id,
                    state.conversation.version,
                    result.state,
                    record,
                    None,
                    result.events,
                )
                return
            except DomainError as exc:
                if exc.code is ErrorCode.OPTIMISTIC_CONFLICT:
                    continue
                return
            except Exception:  # noqa: BLE001
                return

    async def _commit_process(
        self,
        *,
        state: ConversationState,
        process: ProcessRecord,
        launch_history_entry: LaunchSnapshot | None,
        events: tuple[ConversationEvent, ...],
    ) -> None:
        await self._persistence.commit_runtime_lifecycle(
            state.conversation.id,
            state.conversation.version,
            state,
            process,
            launch_history_entry,
            events,
        )

    def _build_sdk_launch_snapshot(
        self,
        *,
        executable_path: str | None,
        working_directory: str,
        workspace_roots: tuple[str, ...],
        capabilities: HarnessCapabilities,
        model: str | None,
        mode: str | None,
        adapter_version: str,
    ) -> LaunchSnapshot:
        """Resolve cwd/roots for SDK-managed runtimes; executable is optional."""
        workdir = resolve_directory(
            working_directory,
            error_code=ErrorCode.WORKING_DIRECTORY_NOT_FOUND,
        )
        roots = tuple(
            resolve_directory(root, error_code=ErrorCode.WORKSPACE_ROOT_NOT_FOUND)
            for root in workspace_roots
        )
        resolved_exe: str | None = None
        if executable_path:
            resolved_exe = str(resolve_executable(executable_path))
        return LaunchSnapshot(
            resolved_executable=resolved_exe,
            harness_version=capabilities.version,
            working_directory=str(workdir),
            workspace_roots=tuple(str(r) for r in roots),
            model=model,
            mode=mode,
            adapter_version=adapter_version,
            capabilities=capabilities,
        )

    async def _lifecycle_pump(
        self,
        managed: ManagedRuntime,
    ) -> None:
        if managed.process is None:
            return
        try:
            async for event in managed.process.events():
                await self._handle_process_event(managed, event)
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001
            logger.exception("lifecycle pump failed for %s", managed.conversation_id)

    async def _handle_process_event(
        self,
        managed: ManagedRuntime,
        event: ProcessEvent,
    ) -> None:
        if managed.closed:
            return
        async with self._lock_for(managed.conversation_id):
            if managed.closed or managed.conversation_id not in self._runtimes:
                return
            await self._persist_process_event(managed, event)

            if isinstance(event, (ProcessExitedEvent, ProcessForcedTerminationEvent)):
                managed.terminal_persisted = True
                await self._teardown_runtime(managed, close_adapter=True)

    async def _persist_process_event(
        self,
        managed: ManagedRuntime,
        event: ProcessEvent,
    ) -> None:
        """Persist one lifecycle event, reallocating sequences after conflicts."""
        while True:
            state = await self._persistence.get_snapshot(
                managed.conversation_id,
                managed.owner_id,
            )
            expected_version = state.conversation.version
            new_state, process, events = self._apply_process_event(managed, state, event)
            if not events:
                return
            try:
                await self._persistence.commit_runtime_lifecycle(
                    managed.conversation_id,
                    expected_version,
                    new_state,
                    process,
                    None,
                    events,
                )
            except DomainError as exc:
                if exc.code is ErrorCode.OPTIMISTIC_CONFLICT:
                    continue
                raise
            managed.process_record = process
            if isinstance(event, ProcessStderrTruncatedEvent):
                managed.stderr_truncation_persisted = True
            return

    def _apply_process_event(
        self,
        managed: ManagedRuntime,
        state: ConversationState,
        event: ProcessEvent,
    ) -> tuple[ConversationState, ProcessRecord, tuple[ConversationEvent, ...]]:
        now = self._clock()
        process = managed.process_record
        handle = managed.process
        payloads: list[EventPayload]
        if isinstance(event, ProcessStderrTruncatedEvent):
            stderr_tail = handle.redacted_stderr_tail if handle is not None else ""
            process = process.model_copy(update={"redacted_stderr_tail": stderr_tail})
            payloads = [
                ProcessStderrTruncatedPayload(
                    process_id=event.process_id,
                    retained_bytes=event.retained_bytes,
                )
            ]
        elif isinstance(event, ProcessSilenceWarningEvent):
            payloads = [
                ProviderWarningPayload(
                    message="no stdout activity within silence window",
                    code="provider_silence",
                )
            ]
        elif isinstance(event, ProcessExitedEvent):
            stderr_tail = handle.redacted_stderr_tail if handle is not None else ""
            # SDK-managed opaque processes (pid=None) exit with code None → EXITED.
            if event.exit_code is None and handle is None or event.exit_code == 0:
                status = ProcessStatus.EXITED
            else:
                status = ProcessStatus.FAILED
            process = process.model_copy(
                update={
                    "status": status,
                    "exit_code": event.exit_code,
                    "exited_at": now,
                    "redacted_stderr_tail": stderr_tail,
                }
            )
            if event.exit_code not in (0, None):
                result = fail_session(
                    state,
                    now=now,
                    error_code="process_exited",
                    message=f"process exited with code {event.exit_code}",
                )
                new_state, more = append_events(
                    result.state,
                    now,
                    [ProcessExitedPayload(process_id=event.process_id, exit_code=event.exit_code)],
                )
                return new_state, process, result.events + more
            payloads = [
                ProcessExitedPayload(process_id=event.process_id, exit_code=event.exit_code)
            ]
        elif isinstance(event, ProcessForcedTerminationEvent):
            stderr_tail = handle.redacted_stderr_tail if handle is not None else ""
            process = process.model_copy(
                update={
                    "status": ProcessStatus.TERMINATED,
                    "exited_at": now,
                    "exit_code": handle.returncode if handle is not None else None,
                    "redacted_stderr_tail": stderr_tail,
                }
            )
            payloads = [
                ProcessForcedTerminationPayload(
                    process_id=event.process_id,
                    reason=event.reason,
                )
            ]
        else:
            return state, process, ()
        new_state, events = append_events(state, now, payloads)
        return new_state, process, events

    async def interrupt(self, conversation_id: UUID) -> None:
        async with self._lock_for(conversation_id):
            managed = self._runtimes.get(conversation_id)
            if managed is None:
                raise DomainError(
                    ErrorCode.INVALID_STATE,
                    "no active runtime for conversation",
                    details={"conversation_id": str(conversation_id)},
                )
            try:
                await asyncio.wait_for(
                    managed.adapter.interrupt(managed.session),
                    timeout=self._policy.interrupt_timeout,
                )
            except TimeoutError:
                if managed.process is not None:
                    await managed.process.force_terminate(reason="interrupt_timeout")
                try:
                    await self._persist_terminal(managed)
                finally:
                    await self._teardown_runtime(managed, close_adapter=False)
                raise DomainError(
                    ErrorCode.RUNTIME_TIMEOUT,
                    "adapter interrupt timed out",
                    details={"conversation_id": str(conversation_id)},
                ) from None
            self._arm_idle_timer(conversation_id)

    async def close(self, conversation_id: UUID, *, reason: str | None = None) -> None:
        async with self._lock_for(conversation_id):
            managed = self._runtimes.get(conversation_id)
            if managed is None:
                return
            await self._close_managed(managed, reason=reason)

    async def _close_managed(
        self,
        managed: ManagedRuntime,
        *,
        reason: str | None,
    ) -> None:
        if managed.closed:
            return
        try:
            await asyncio.wait_for(
                managed.adapter.close(managed.session),
                timeout=self._policy.graceful_close_timeout,
            )
        except TimeoutError:
            if managed.process is not None:
                await managed.process.force_terminate(reason="graceful_close_timeout")
        else:
            if managed.process is not None:
                await managed.process.close()
        # Persist terminal status for both process-bound and SDK-managed runtimes.
        await self._persist_terminal(managed, session_action="close", reason=reason)
        await self._teardown_runtime(managed, close_adapter=False)

    async def reap_if_eligible(self, conversation_id: UUID) -> bool:
        """Re-read authoritative state; reap only when idle_reap_eligible."""
        async with self._lock_for(conversation_id):
            managed = self._runtimes.get(conversation_id)
            if managed is None:
                return False
            state = await self._persistence.get_snapshot(
                conversation_id,
                managed.owner_id,
            )
            if not state.idle_reap_eligible:
                return False
            # Close live resources; preserve native resume ID and launch history.
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    managed.adapter.close(managed.session),
                    timeout=self._policy.graceful_close_timeout,
                )
            if managed.process is not None:
                await managed.process.close()
            await self._persist_terminal(managed, session_action="reap", reason="idle")
            await self._teardown_runtime(managed, close_adapter=False)
            return True

    async def _persist_terminal(
        self,
        managed: ManagedRuntime,
        *,
        session_action: str | None = None,
        reason: str | None = None,
    ) -> None:
        if managed.terminal_persisted:
            return
        handle = managed.process
        event: ProcessEvent
        if handle is not None and handle.forced:
            event = ProcessForcedTerminationEvent(
                process_id=managed.process_record.id,
                reason=handle.forced_reason or reason,
            )
        else:
            event = ProcessExitedEvent(
                process_id=managed.process_record.id,
                exit_code=handle.returncode if handle is not None else None,
            )

        while True:
            state = await self._persistence.get_snapshot(
                managed.conversation_id,
                managed.owner_id,
            )
            expected_version = state.conversation.version
            prior_events: tuple[ConversationEvent, ...] = ()
            if (
                handle is not None
                and handle.stderr_truncated
                and not managed.stderr_truncation_persisted
            ):
                state, _, prior_events = self._apply_process_event(
                    managed,
                    state,
                    ProcessStderrTruncatedEvent(
                        process_id=managed.process_record.id,
                        retained_bytes=handle.retained_stderr_bytes,
                    ),
                )
            new_state, process, process_events = self._apply_process_event(
                managed,
                state,
                event,
            )
            session_events: tuple[ConversationEvent, ...] = ()
            if session_action == "close":
                result = close_session(new_state, now=self._clock(), reason=reason)
                new_state, session_events = result.state, result.events
            elif session_action == "reap":
                result = reap_session(new_state, now=self._clock(), reason=reason)
                new_state, session_events = result.state, result.events
            try:
                await self._persistence.commit_runtime_lifecycle(
                    managed.conversation_id,
                    expected_version,
                    new_state,
                    process,
                    None,
                    prior_events + process_events + session_events,
                )
            except DomainError as exc:
                if exc.code is ErrorCode.OPTIMISTIC_CONFLICT:
                    continue
                raise
            managed.process_record = process
            managed.terminal_persisted = True
            if prior_events:
                managed.stderr_truncation_persisted = True
            return

    def _arm_idle_timer(self, conversation_id: UUID) -> None:
        existing = self._idle_tasks.pop(conversation_id, None)
        if existing is not None:
            existing.cancel()

        async def _idle() -> None:
            try:
                await asyncio.sleep(self._policy.idle_reap)
                await self.reap_if_eligible(conversation_id)
            except asyncio.CancelledError:
                return

        self._idle_tasks[conversation_id] = asyncio.create_task(
            _idle(),
            name=f"idle-reap-{conversation_id}",
        )

    async def _teardown_runtime(
        self,
        managed: ManagedRuntime,
        *,
        close_adapter: bool,
    ) -> None:
        if managed.closed:
            return
        managed.closed = True
        idle = self._idle_tasks.pop(managed.conversation_id, None)
        if idle is not None:
            idle.cancel()
        current = asyncio.current_task()
        others = [t for t in managed.tasks if t is not current]
        for task in others:
            task.cancel()
        if others:
            await asyncio.gather(*others, return_exceptions=True)
        managed.tasks.clear()
        if close_adapter:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    managed.adapter.close(managed.session),
                    timeout=self._policy.graceful_close_timeout,
                )
        if managed.process is not None:
            with contextlib.suppress(Exception):
                if managed.process.returncode is None:
                    await managed.process.force_terminate(reason="teardown")
                else:
                    await managed.process.close()
        self._runtimes.pop(managed.conversation_id, None)

    async def shutdown(self) -> None:
        """Idempotent shutdown: reject new runtimes, interrupt, then force-kill."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._policy.shutdown_budget
        force_reserve = min(
            self._policy.terminate_escalation + 0.25,
            self._policy.shutdown_budget / 2,
        )
        graceful_deadline = deadline - force_reserve
        async with self._global_lock:
            already_shutting_down = self._shutting_down
            self._shutting_down = True
            startups = list(self._startup_tasks)
        if already_shutting_down:
            await self._force_all(deadline)
            return

        # Cancel admitted starts before taking the runtime snapshot. Their
        # cancellation path terminates any child that has already been spawned.
        for task in startups:
            task.cancel()
        if startups:
            remaining = max(0.0, graceful_deadline - loop.time())
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    asyncio.gather(*startups, return_exceptions=True),
                    timeout=remaining,
                )

        conversations = list(self._runtimes)

        async def _interrupt_one(cid: UUID) -> None:
            with contextlib.suppress(Exception):
                await self.interrupt(cid)

        remaining = max(0.0, graceful_deadline - loop.time())
        if conversations and remaining > 0:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    asyncio.gather(*[_interrupt_one(c) for c in conversations]),
                    timeout=remaining,
                )

        remaining = max(0.0, graceful_deadline - loop.time())
        if remaining > 0 and self._runtimes:

            async def _close_one(cid: UUID) -> None:
                with contextlib.suppress(Exception):
                    await self.close(cid, reason="shutdown")

            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    asyncio.gather(*[_close_one(c) for c in list(self._runtimes)]),
                    timeout=remaining,
                )

        await self._force_all(deadline)

    async def _force_all(self, deadline: float) -> None:
        async def _force_one(managed: ManagedRuntime) -> None:
            async with self._lock_for(managed.conversation_id):
                if managed.closed:
                    return
                if managed.process is not None:
                    with contextlib.suppress(Exception):
                        await managed.process.force_terminate(reason="shutdown")
                else:
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(
                            managed.adapter.close(managed.session),
                            timeout=self._policy.graceful_close_timeout,
                        )
                with contextlib.suppress(Exception):
                    await self._persist_terminal(
                        managed,
                        session_action="close",
                        reason="shutdown",
                    )
                with contextlib.suppress(Exception):
                    await self._teardown_runtime(managed, close_adapter=False)

        force_tasks = [
            asyncio.create_task(_force_one(managed), name=f"force-{managed.conversation_id}")
            for managed in list(self._runtimes.values())
        ]
        remaining = max(0.0, deadline - asyncio.get_running_loop().time())
        if force_tasks and remaining > 0:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    asyncio.gather(*force_tasks, return_exceptions=True),
                    timeout=remaining,
                )
        for task in force_tasks:
            if not task.done():
                task.cancel()
        for task in list(self._idle_tasks.values()):
            task.cancel()
        self._idle_tasks.clear()
        self._runtimes.clear()

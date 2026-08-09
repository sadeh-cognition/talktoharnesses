"""RuntimeManager — one supervised runtime per conversation."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal, cast
from uuid import UUID, uuid4

from talktoharnesses.application.faults import FaultCallback, FaultPoint, checkpoint
from talktoharnesses.application.observability import get_observability
from talktoharnesses.application.persistence import Persistence
from talktoharnesses.domain.enums import ErrorCode, HarnessKind, ProcessStatus, RecoveryReasonCode
from talktoharnesses.domain.errors import DomainError, public_message
from talktoharnesses.domain.events import (
    ConversationEvent,
    EventPayload,
    InteractionRequestedPayload,
    ProcessExitedPayload,
    ProcessForcedTerminationPayload,
    ProcessStderrTruncatedPayload,
    ProviderWarningPayload,
    TurnCompletedPayload,
    TurnFailedPayload,
    TurnInterruptedPayload,
    TurnOutcomeUnknownPayload,
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
    HarnessInteractionRequest,
    HarnessSession,
    ResumeSessionRequest,
    StartSessionRequest,
    TurnRequest,
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


def _preflight_process_operation(
    adapter: HarnessAdapter,
    mode: Literal["create", "resume"],
) -> None:
    preflight = getattr(adapter, "preflight_operation", None)
    if callable(preflight):
        preflight(mode)


def _map_resume_reason(exc: DomainError) -> RecoveryReasonCode:
    if exc.code is ErrorCode.PROVIDER_INCOMPATIBLE:
        message = exc.message
        if message == RecoveryReasonCode.RESUME_UNSUPPORTED.value:
            return RecoveryReasonCode.RESUME_UNSUPPORTED
        return RecoveryReasonCode.PROVIDER_INCOMPATIBLE
    if exc.code is ErrorCode.RUNTIME_TIMEOUT:
        return RecoveryReasonCode.RESUME_REJECTED
    return RecoveryReasonCode.RESUME_REJECTED


@dataclass(frozen=True)
class _LaunchPlan:
    """Adapter and resolved spawn inputs shared by live and candidate starts."""

    adapter: HarnessAdapter
    sdk_managed: bool
    executable_path: str | None
    argv: tuple[str, ...]


@dataclass
class ManagedRuntime:
    conversation_id: UUID
    owner_id: str
    adapter: HarnessAdapter
    session: HarnessSession
    process: ProcessHandle | None
    process_record: ProcessRecord
    launch: LaunchSnapshot
    worker_id: str | None = None
    fence: int | None = None
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
        fault_callback: FaultCallback = None,
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
        self._fault_callback = fault_callback

        self._runtimes: dict[UUID, ManagedRuntime] = {}
        # Transient candidates keyed by their prospective binding ID.
        self._candidates: dict[UUID, ManagedRuntime] = {}
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
        worker_id: str | None = None,
        fence: int | None = None,
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
            worker_id=worker_id,
            fence=fence,
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
        worker_id: str | None = None,
        fence: int | None = None,
    ) -> HarnessSession:
        return await self._start_request(
            conversation_id=conversation_id,
            owner_id=owner_id,
            configuration=configuration,
            argv=argv,
            adapter_version=adapter_version,
            executable_path=executable_path,
            resume_native_id=native_session_id,
            worker_id=worker_id,
            fence=fence,
        )

    async def prepare_launch_snapshot(
        self,
        configuration: HarnessConfiguration,
        *,
        argv: tuple[str, ...] = (),
        adapter_version: str = "0",
        executable_path: str | None = None,
    ) -> LaunchSnapshot:
        """Probe and build a prospective launch snapshot without mutating bindings."""
        plan = self._plan_launch(
            configuration=configuration,
            argv=argv,
            executable_path=executable_path,
        )
        return await self._probe_and_build_launch(
            plan,
            configuration=configuration,
            adapter_version=adapter_version,
        )

    async def resume_for_recovery(
        self,
        conversation_id: UUID,
        owner_id: str,
        configuration: HarnessConfiguration,
        native_session_id: str,
        *,
        worker_id: str,
        fence: int,
        expected_binding_kind: HarnessKind,
        previous_launch: LaunchSnapshot | None,
        argv: tuple[str, ...] = (),
        adapter_version: str = "0",
        executable_path: str | None = None,
    ) -> tuple[ManagedRuntime, RecoveryReasonCode]:
        """Create a fresh local runtime and native-resume under a fence.

        Never attaches to a prior PID. Commits lifecycle under the fence before
        installing the runtime into the live map.
        """
        task = asyncio.current_task()
        assert task is not None
        async with self._global_lock:
            if self._shutting_down:
                raise DomainError(ErrorCode.INVALID_STATE, "runtime manager is shutting down")
            if conversation_id not in self._runtimes:
                self._require_capacity()
            self._startup_tasks.add(task)
        try:
            async with self._lock_for(conversation_id):
                if conversation_id in self._runtimes:
                    raise DomainError(
                        ErrorCode.CONVERSATION_BUSY,
                        "conversation already has an active runtime",
                        details={"conversation_id": str(conversation_id)},
                    )
                if configuration.kind != expected_binding_kind:
                    raise DomainError(
                        ErrorCode.INVALID_STATE,
                        RecoveryReasonCode.INVARIANT_FAILURE.value,
                        details={"conversation_id": str(conversation_id)},
                    )
                return await self._resume_for_recovery_locked(
                    conversation_id=conversation_id,
                    owner_id=owner_id,
                    configuration=configuration,
                    native_session_id=native_session_id,
                    worker_id=worker_id,
                    fence=fence,
                    previous_launch=previous_launch,
                    argv=argv,
                    adapter_version=adapter_version,
                    executable_path=executable_path,
                )
        finally:
            async with self._global_lock:
                self._startup_tasks.discard(task)

    async def _resume_for_recovery_locked(
        self,
        *,
        conversation_id: UUID,
        owner_id: str,
        configuration: HarnessConfiguration,
        native_session_id: str,
        worker_id: str,
        fence: int,
        previous_launch: LaunchSnapshot | None,
        argv: tuple[str, ...],
        adapter_version: str,
        executable_path: str | None,
    ) -> tuple[ManagedRuntime, RecoveryReasonCode]:
        state = await self._persistence.get_worker_snapshot(conversation_id)
        if state.binding is None:
            raise DomainError(ErrorCode.INVALID_STATE, "conversation has no binding")
        binding = state.binding
        plan = self._plan_launch(
            configuration=configuration,
            argv=argv,
            executable_path=executable_path,
        )
        try:
            launch = await self._probe_and_build_launch(
                plan,
                configuration=configuration,
                adapter_version=adapter_version,
            )
            _preflight_process_operation(plan.adapter, "resume")
        except DomainError as exc:
            raise DomainError(
                ErrorCode.PROVIDER_INCOMPATIBLE,
                RecoveryReasonCode.PROVIDER_INCOMPATIBLE.value,
                details={"conversation_id": str(conversation_id)},
            ) from exc

        if not launch.capabilities.supports_resume:
            raise DomainError(
                ErrorCode.PROVIDER_INCOMPATIBLE,
                RecoveryReasonCode.RESUME_UNSUPPORTED.value,
                details={"conversation_id": str(conversation_id)},
            )

        reason = (
            RecoveryReasonCode.UNCHANGED_LAUNCH
            if previous_launch is not None
            and previous_launch.resolved_executable == launch.resolved_executable
            and previous_launch.harness_version == launch.harness_version
            and previous_launch.adapter_version == launch.adapter_version
            else RecoveryReasonCode.EXECUTABLE_CHANGED
        )

        process_id = uuid4()
        process_record = ProcessRecord(
            id=process_id,
            conversation_id=conversation_id,
            binding_id=binding.id,
            status=ProcessStatus.STARTING,
        )
        await self._persistence.commit_runtime_lifecycle(
            conversation_id,
            state.conversation.version,
            state,
            process_record,
            None,
            (),
            worker_id=worker_id,
            fence=fence,
        )
        state = await self._persistence.get_worker_snapshot(conversation_id)

        handle: ProcessHandle | None = None
        try:
            if not plan.sdk_managed:
                handle = await self._spawn_process(
                    plan,
                    conversation_id=conversation_id,
                    binding_id=binding.id,
                    process_id=process_id,
                    launch=launch,
                )
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
            await self._persistence.commit_runtime_lifecycle(
                conversation_id,
                state.conversation.version,
                state,
                process_record,
                launch,
                (),
                worker_id=worker_id,
                fence=fence,
            )
            state = await self._persistence.get_worker_snapshot(conversation_id)

            try:
                session = await asyncio.wait_for(
                    plan.adapter.resume(
                        ResumeSessionRequest(
                            conversation_id=conversation_id,
                            binding_id=binding.id,
                            configuration=configuration,
                            native_session_id=native_session_id,
                            launch=launch,
                        )
                    ),
                    timeout=self._policy.start_resume_timeout,
                )
            except TimeoutError as exc:
                raise DomainError(
                    ErrorCode.RUNTIME_TIMEOUT,
                    RecoveryReasonCode.RESUME_REJECTED.value,
                    details={"conversation_id": str(conversation_id)},
                ) from exc
            except DomainError as exc:
                mapped = _map_resume_reason(exc)
                raise DomainError(
                    exc.code,
                    mapped.value,
                    details={"conversation_id": str(conversation_id)},
                ) from exc

            result = resume_session(
                state,
                now=self._clock(),
                native_session_id=session.native_session_id or native_session_id,
                launch=launch,
            )
            await self._persistence.commit_runtime_lifecycle(
                conversation_id,
                state.conversation.version,
                result.state,
                process_record,
                None,
                result.events,
                worker_id=worker_id,
                fence=fence,
            )
            get_observability().observe_committed_events(result.events, state=result.state)
            await checkpoint(self._fault_callback, FaultPoint.AFTER_NATIVE_RESUME_COMMIT)

            managed = ManagedRuntime(
                conversation_id=conversation_id,
                owner_id=owner_id,
                adapter=plan.adapter,
                session=session,
                process=handle,
                process_record=process_record,
                launch=launch,
                worker_id=worker_id,
                fence=fence,
            )
            if handle is not None:
                pump = asyncio.create_task(
                    self._lifecycle_pump(managed),
                    name=f"lifecycle-{conversation_id}",
                )
                managed.tasks.append(pump)
            self._runtimes[conversation_id] = managed
            self._arm_idle_timer(conversation_id)
            return managed, reason
        except BaseException:
            if handle is not None:
                with contextlib.suppress(Exception):
                    await handle.force_terminate(reason="recovery_resume_failure")
            elif plan.sdk_managed:
                await self._rollback_sdk_startup(
                    plan.adapter,
                    conversation_id=conversation_id,
                    binding_id=binding.id,
                    configuration=configuration,
                )
            raise

    async def recovery_handoff_fallback(
        self,
        conversation_id: UUID,
        owner_id: str,
        binding_id: UUID,
        configuration: HarnessConfiguration,
        handoff_text: str,
        *,
        worker_id: str,
        fence: int,
    ) -> ManagedRuntime | None:
        """Start/seed a candidate for recovery fallback; caller commits rotation.

        On rejection closes the candidate and marks requires_session_recreation.
        """
        try:
            candidate = await self.start_candidate(
                conversation_id=conversation_id,
                owner_id=owner_id,
                binding_id=binding_id,
                configuration=configuration,
                worker_id=worker_id,
                fence=fence,
            )
            await self.seed_candidate(candidate, handoff_text)
            await checkpoint(self._fault_callback, FaultPoint.AFTER_FALLBACK_SEED)
            return candidate
        except Exception:
            with contextlib.suppress(Exception):
                await self.close_candidate(binding_id)
            try:
                state = await self._persistence.get_worker_snapshot(conversation_id)
                await self._persistence.commit_rotation_requires_recreation(
                    conversation_id,
                    state.conversation.version,
                    worker_id=worker_id,
                    fence=fence,
                )
            except Exception:
                logger.warning(
                    "recovery_fallback_recreation_flag_failed conversation=%s",
                    conversation_id,
                )
            return None

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
        worker_id: str | None,
        fence: int | None,
    ) -> HarnessSession:
        task = asyncio.current_task()
        assert task is not None
        async with self._global_lock:
            if self._shutting_down:
                raise DomainError(ErrorCode.INVALID_STATE, "runtime manager is shutting down")
            if conversation_id not in self._runtimes:
                self._require_capacity()
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
                    worker_id=worker_id,
                    fence=fence,
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
        worker_id: str | None,
        fence: int | None,
    ) -> HarnessSession:
        state = await self._persistence.get_snapshot(conversation_id, owner_id)
        if state.binding is None:
            raise DomainError(ErrorCode.INVALID_STATE, "conversation has no binding")

        binding = state.binding
        plan = self._plan_launch(
            configuration=configuration,
            argv=argv,
            executable_path=executable_path,
        )
        adapter = plan.adapter
        sdk_managed = plan.sdk_managed

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
                worker_id=worker_id,
                fence=fence,
            )
            state = await self._persistence.get_snapshot(conversation_id, owner_id)
        except DomainError:
            raise

        handle: ProcessHandle | None = None
        launch: LaunchSnapshot | None = None
        try:
            launch = await self._probe_and_build_launch(
                plan,
                configuration=configuration,
                adapter_version=adapter_version,
            )
            _preflight_process_operation(
                adapter,
                "create" if resume_native_id is None else "resume",
            )
            if not sdk_managed:
                handle = await self._spawn_process(
                    plan,
                    conversation_id=conversation_id,
                    binding_id=binding.id,
                    process_id=process_id,
                    launch=launch,
                )

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
                worker_id=worker_id,
                fence=fence,
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
                        worker_id=worker_id,
                        fence=fence,
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
                        worker_id=worker_id,
                        fence=fence,
                    )
                    state = await self._persistence.get_snapshot(conversation_id, owner_id)
                    retry_argv = tuple(str(part) for part in retry_argv_obj)
                    handle = None
                    handle = await self._spawn_process(
                        plan,
                        conversation_id=conversation_id,
                        binding_id=binding.id,
                        process_id=process_id,
                        launch=launch,
                        argv=retry_argv,
                    )
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
                        worker_id=worker_id,
                        fence=fence,
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
                worker_id=worker_id,
                fence=fence,
            )
            get_observability().observe_committed_events(result.events, state=result.state)
            state = await self._persistence.get_snapshot(conversation_id, owner_id)

            managed = ManagedRuntime(
                conversation_id=conversation_id,
                owner_id=owner_id,
                adapter=adapter,
                session=session,
                process=handle,
                process_record=process_record,
                launch=launch,
                worker_id=worker_id,
                fence=fence,
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
                        worker_id=worker_id,
                        fence=fence,
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
                        worker_id=worker_id,
                        fence=fence,
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
                worker_id=worker_id,
                fence=fence,
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
                public_message(exc.code),
                worker_id=worker_id,
                fence=fence,
            )
            raise
        except Exception:
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
                public_message(ErrorCode.INVALID_STATE),
                worker_id=worker_id,
                fence=fence,
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
        *,
        worker_id: str | None = None,
        fence: int | None = None,
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
                    worker_id=worker_id,
                    fence=fence,
                )
                get_observability().observe_committed_events(result.events, state=result.state)
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
        worker_id: str | None = None,
        fence: int | None = None,
    ) -> None:
        await self._persistence.commit_runtime_lifecycle(
            state.conversation.id,
            state.conversation.version,
            state,
            process,
            launch_history_entry,
            events,
            worker_id=worker_id,
            fence=fence,
        )
        get_observability().observe_committed_events(events, state=state)

    def _plan_launch(
        self,
        *,
        configuration: HarnessConfiguration,
        argv: tuple[str, ...],
        executable_path: str | None,
    ) -> _LaunchPlan:
        """Create the adapter and resolve the executable/argv it will launch with."""
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
        return _LaunchPlan(
            adapter=adapter,
            sdk_managed=sdk_managed,
            executable_path=exe,
            argv=effective_argv,
        )

    async def _probe_and_build_launch(
        self,
        plan: _LaunchPlan,
        *,
        configuration: HarnessConfiguration,
        adapter_version: str,
    ) -> LaunchSnapshot:
        caps = await asyncio.wait_for(
            plan.adapter.probe(configuration),
            timeout=self._policy.start_resume_timeout,
        )
        if plan.sdk_managed:
            return self._build_sdk_launch_snapshot(
                executable_path=plan.executable_path,
                working_directory=configuration.working_directory,
                workspace_roots=configuration.workspace_roots,
                capabilities=caps,
                model=configuration.model,
                mode=configuration.mode,
                adapter_version=adapter_version,
            )
        assert plan.executable_path is not None
        return self._supervisor.build_launch_snapshot(
            executable_path=plan.executable_path,
            working_directory=configuration.working_directory,
            workspace_roots=configuration.workspace_roots,
            capabilities=caps,
            model=configuration.model,
            mode=configuration.mode,
            adapter_version=adapter_version,
        )

    async def _spawn_process(
        self,
        plan: _LaunchPlan,
        *,
        conversation_id: UUID,
        binding_id: UUID,
        process_id: UUID,
        launch: LaunchSnapshot,
        argv: tuple[str, ...] | None = None,
    ) -> ProcessHandle:
        handle = await self._supervisor.spawn(
            ProcessSpec(
                conversation_id=conversation_id,
                binding_id=binding_id,
                process_id=process_id,
                launch=launch,
                argv=plan.argv if argv is None else argv,
            ),
            redaction_patterns=self._redaction_patterns,
        )
        bind_process = getattr(plan.adapter, "bind_process", None)
        if callable(bind_process):
            bind_process(handle)
        return handle

    # ------------------------------------------------------------------
    # Candidate runtimes (durable switching and post-retention rotation)
    # ------------------------------------------------------------------

    def get_candidate(self, binding_id: UUID) -> ManagedRuntime | None:
        return self._candidates.get(binding_id)

    async def start_candidate(
        self,
        *,
        conversation_id: UUID,
        owner_id: str,
        binding_id: UUID,
        configuration: HarnessConfiguration,
        argv: tuple[str, ...] = (),
        adapter_version: str = "0",
        executable_path: str | None = None,
        worker_id: str | None = None,
        fence: int | None = None,
    ) -> ManagedRuntime:
        """Start a transient runtime with a new native session for ``binding_id``.

        The candidate is never inserted into the live conversation map and
        writes no lifecycle rows: the current binding stays authoritative until
        the caller commits the switch and calls :meth:`promote_candidate`.
        """
        async with self._global_lock:
            if self._shutting_down:
                raise DomainError(ErrorCode.INVALID_STATE, "runtime manager is shutting down")
            if binding_id in self._candidates:
                raise DomainError(
                    ErrorCode.CONVERSATION_BUSY,
                    "binding already has a candidate runtime",
                    details={"binding_id": str(binding_id)},
                )
            self._require_capacity()

        plan = self._plan_launch(
            configuration=configuration,
            argv=argv,
            executable_path=executable_path,
        )
        process_id = uuid4()
        handle: ProcessHandle | None = None
        try:
            launch = await self._probe_and_build_launch(
                plan,
                configuration=configuration,
                adapter_version=adapter_version,
            )
            _preflight_process_operation(plan.adapter, "create")
            if not plan.sdk_managed:
                handle = await self._spawn_process(
                    plan,
                    conversation_id=conversation_id,
                    binding_id=binding_id,
                    process_id=process_id,
                    launch=launch,
                )
            # Candidates always create a new native session; never resume.
            session = await asyncio.wait_for(
                plan.adapter.start(
                    StartSessionRequest(
                        conversation_id=conversation_id,
                        binding_id=binding_id,
                        configuration=configuration,
                        launch=launch,
                    )
                ),
                timeout=self._policy.start_resume_timeout,
            )
        except TimeoutError as exc:
            await self._abort_candidate_startup(
                plan,
                handle,
                conversation_id=conversation_id,
                binding_id=binding_id,
                configuration=configuration,
            )
            raise DomainError(
                ErrorCode.RUNTIME_TIMEOUT,
                "candidate session start timed out",
                details={"conversation_id": str(conversation_id)},
            ) from exc
        except BaseException:
            await asyncio.shield(
                self._abort_candidate_startup(
                    plan,
                    handle,
                    conversation_id=conversation_id,
                    binding_id=binding_id,
                    configuration=configuration,
                )
            )
            raise

        managed = ManagedRuntime(
            conversation_id=conversation_id,
            owner_id=owner_id,
            adapter=plan.adapter,
            session=session,
            process=handle,
            process_record=ProcessRecord(
                id=process_id,
                conversation_id=conversation_id,
                binding_id=binding_id,
                status=ProcessStatus.RUNNING,
                pid=handle.pid if handle is not None else None,
                started_at=self._clock(),
            ),
            launch=launch,
            worker_id=worker_id,
            fence=fence,
        )
        self._candidates[binding_id] = managed
        return managed

    async def seed_candidate(
        self,
        managed: ManagedRuntime,
        handoff_text: str,
        *,
        timeout: float | None = None,
    ) -> None:
        """Submit the retained handoff as one synthetic turn and drain its terminal.

        Candidate content events are discarded: nothing seeded here is
        materialized or published. Any interaction request, non-successful
        terminal, foreign turn, timeout, or stream end rejects the candidate.
        """
        if not handoff_text:
            return
        budget = self._policy.start_resume_timeout if timeout is None else timeout
        turn_id = uuid4()
        try:
            await asyncio.wait_for(
                self._drain_seed(managed, handoff_text, turn_id=turn_id),
                timeout=budget,
            )
        except TimeoutError as exc:
            raise DomainError(
                ErrorCode.RUNTIME_TIMEOUT,
                "candidate handoff seeding timed out",
                details={"conversation_id": str(managed.conversation_id)},
            ) from exc

    async def _drain_seed(
        self,
        managed: ManagedRuntime,
        handoff_text: str,
        *,
        turn_id: UUID,
    ) -> None:
        await managed.adapter.submit(
            managed.session,
            TurnRequest(turn_id=turn_id, command_id=uuid4(), prompt=handoff_text),
        )
        async for event in managed.adapter.events(managed.session):
            if isinstance(event, (HarnessInteractionRequest, InteractionRequestedPayload)):
                raise DomainError(
                    ErrorCode.PROTOCOL_ERROR,
                    "candidate requested an interaction while seeding the handoff",
                )
            event_turn = getattr(event, "turn_id", None)
            if isinstance(event_turn, UUID) and event_turn != turn_id:
                raise DomainError(
                    ErrorCode.PROTOCOL_ERROR,
                    "candidate emitted an event for an unexpected turn",
                )
            if isinstance(event, TurnCompletedPayload):
                return
            if isinstance(
                event,
                (TurnFailedPayload, TurnInterruptedPayload, TurnOutcomeUnknownPayload),
            ):
                raise DomainError(
                    ErrorCode.PROTOCOL_ERROR,
                    f"candidate handoff turn ended as {event.type}",
                )
        raise DomainError(
            ErrorCode.PROTOCOL_ERROR,
            "candidate event stream ended before the handoff turn terminated",
        )

    async def promote_candidate(self, conversation_id: UUID, binding_id: UUID) -> ManagedRuntime:
        """Install a committed candidate as the conversation's live runtime."""
        async with self._lock_for(conversation_id):
            managed = self._candidates.pop(binding_id, None)
            if managed is None:
                raise DomainError(
                    ErrorCode.INVALID_STATE,
                    "no candidate runtime for binding",
                    details={"binding_id": str(binding_id)},
                )
            self._runtimes[conversation_id] = managed
            if managed.process is not None:
                managed.tasks.append(
                    asyncio.create_task(
                        self._lifecycle_pump(managed),
                        name=f"lifecycle-{conversation_id}",
                    )
                )
            self._arm_idle_timer(conversation_id)
            return managed

    async def close_candidate(self, binding_id: UUID) -> None:
        """Shut a rejected candidate down; it owns no durable rows to settle."""
        managed = self._candidates.pop(binding_id, None)
        if managed is None:
            return
        managed.closed = True
        try:
            await asyncio.wait_for(
                managed.adapter.close(managed.session),
                timeout=self._policy.graceful_close_timeout,
            )
        except Exception:  # noqa: BLE001
            if managed.process is not None:
                with contextlib.suppress(Exception):
                    await managed.process.force_terminate(reason="candidate_rejected")
        else:
            if managed.process is not None:
                with contextlib.suppress(Exception):
                    await managed.process.close()

    async def close_replaced_runtime(
        self,
        managed: ManagedRuntime,
        *,
        reason: str = "harness_switch",
    ) -> None:
        """Close a runtime already replaced by a promoted candidate.

        Only the process incarnation is settled: the session-close transition
        would otherwise attribute the close to the new active binding.
        """
        async with self._lock_for(managed.conversation_id):
            await self._close_managed(managed, reason=reason, session_action=None)

    async def ensure_binding_current(
        self,
        conversation_id: UUID,
        state: ConversationState,
    ) -> ManagedRuntime | None:
        """Return the live runtime only when its session matches ``state.binding``.

        A separately scheduled cleanup can invalidate a native session while an
        idle runtime still holds it, so a mismatch or pending recreation closes
        the runtime and forces a fresh start.
        """
        managed = self._runtimes.get(conversation_id)
        if managed is None:
            return None
        binding = state.binding
        if (
            binding is not None
            and managed.session.binding_id == binding.id
            and managed.session.native_session_id == binding.native_session_id
            and not binding.requires_session_recreation
        ):
            return managed
        logger.info("closing stale runtime for conversation %s", conversation_id)
        await self.close_replaced_runtime(managed, reason="stale_binding")
        return None

    def _require_capacity(self) -> None:
        if len(self._runtimes) + len(self._candidates) >= self._policy.max_runtimes:
            raise DomainError(
                ErrorCode.CONVERSATION_BUSY,
                "runtime capacity reached",
                details={"max_runtimes": str(self._policy.max_runtimes)},
            )

    async def _abort_candidate_startup(
        self,
        plan: _LaunchPlan,
        handle: ProcessHandle | None,
        *,
        conversation_id: UUID,
        binding_id: UUID,
        configuration: HarnessConfiguration,
    ) -> None:
        if plan.sdk_managed:
            await self._rollback_sdk_startup(
                plan.adapter,
                conversation_id=conversation_id,
                binding_id=binding_id,
                configuration=configuration,
            )
        if handle is not None:
            with contextlib.suppress(Exception):
                await handle.force_terminate(reason="candidate_startup_failure")

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
            if managed.closed or self._runtimes.get(managed.conversation_id) is not managed:
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
                    worker_id=managed.worker_id,
                    fence=managed.fence,
                )
            except DomainError as exc:
                if exc.code is ErrorCode.OPTIMISTIC_CONFLICT:
                    continue
                raise
            managed.process_record = process
            get_observability().observe_committed_events(events, state=new_state)
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
        session_action: str | None = "close",
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
        await self._persist_terminal(managed, session_action=session_action, reason=reason)
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
                    worker_id=managed.worker_id,
                    fence=managed.fence,
                )
            except DomainError as exc:
                if exc.code is ErrorCode.OPTIMISTIC_CONFLICT:
                    continue
                raise
            managed.process_record = process
            get_observability().observe_committed_events(
                prior_events + process_events + session_events,
                state=new_state,
            )
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
        replaced = self._runtimes.get(managed.conversation_id) is not managed
        if not replaced:
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
        if not replaced:
            self._runtimes.pop(managed.conversation_id, None)

    async def shutdown(self, *, deadline: float | None = None) -> None:
        """Idempotent shutdown: reject new runtimes, interrupt, then force-kill."""
        loop = asyncio.get_running_loop()
        if deadline is None:
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
        for binding_id in list(self._candidates):
            with contextlib.suppress(Exception):
                await self.close_candidate(binding_id)

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

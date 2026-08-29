"""RuntimeManager — one supervised runtime per conversation."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol, cast
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
from talktoharnesses.runtime.paths import resolve_directory
from talktoharnesses.runtime.policy import RuntimePolicy

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _empty_tasks() -> list[asyncio.Task[None]]:
    return []


class SupervisedProcess(Protocol):
    """Surface RuntimeManager reads from a supervised process.

    Satisfied by the local ``ProcessHandle`` and by ``RemoteProcessHandle``
    (a mirror of a split-service-supervised process).
    """

    @property
    def pid(self) -> int | None: ...

    @property
    def returncode(self) -> int | None: ...

    @property
    def redacted_stderr_tail(self) -> str: ...

    @property
    def forced(self) -> bool: ...

    @property
    def forced_reason(self) -> str | None: ...

    @property
    def stderr_truncated(self) -> bool: ...

    @property
    def retained_stderr_bytes(self) -> int: ...

    def events(self) -> AsyncIterator[ProcessEvent]: ...

    async def close(self) -> None: ...

    async def force_terminate(self, *, reason: str | None = "forced") -> None: ...


def _remote_process_handle(adapter: HarnessAdapter) -> SupervisedProcess | None:
    """A remote adapter exposes the split-supervised process after start/resume."""
    handle = getattr(adapter, "process_handle", None)
    if handle is None:
        return None
    return cast(SupervisedProcess, handle)


async def _await_start_resume(
    adapter: HarnessAdapter,
    operation: Awaitable[HarnessSession],
    *,
    timeout: float,
) -> HarnessSession:
    """Let remote splits apply their staged probe/spawn/start budgets."""
    if getattr(adapter, "remote", False) is True:
        return await operation
    return await asyncio.wait_for(operation, timeout=timeout)


def _import_remote_seen(
    adapter: HarnessAdapter,
    seen_native_ids: frozenset[str],
    seen_stream_offsets: frozenset[str],
) -> None:
    """Seed a remote adapter's dedupe state before session create.

    The split must import seen sets before ``resume`` so replayed native events
    are deduplicated at the source; local adapters keep today's pump-time
    import untouched.
    """
    if getattr(adapter, "remote", False) is not True:
        return
    import_seen = getattr(adapter, "import_seen", None)
    if callable(import_seen):
        cast(
            Callable[[frozenset[str], frozenset[str]], None],
            import_seen,
        )(seen_native_ids, seen_stream_offsets)


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
    """Adapter prepared for a live or candidate start (remote splits own spawn)."""

    adapter: HarnessAdapter


@dataclass
class ManagedRuntime:
    conversation_id: UUID
    owner_id: str
    adapter: HarnessAdapter
    session: HarnessSession
    process: SupervisedProcess | None
    process_record: ProcessRecord
    launch: LaunchSnapshot
    worker_id: str | None = None
    fence: int | None = None
    tasks: list[asyncio.Task[None]] = field(default_factory=_empty_tasks)
    closing: bool = False
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
        clock: Callable[[], datetime] | None = None,
        redaction_patterns: tuple[str, ...] = (),
        fault_callback: FaultCallback = None,
    ) -> None:
        self._persistence = persistence
        self._registry = registry
        self._policy = policy or RuntimePolicy()
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
        managed = self._runtimes.get(conversation_id)
        if managed is None or managed.closing or managed.closed:
            return None
        return managed

    async def start(
        self,
        *,
        conversation_id: UUID,
        owner_id: str,
        configuration: HarnessConfiguration,
        adapter_version: str = "0",
        worker_id: str | None = None,
        fence: int | None = None,
    ) -> HarnessSession:
        """Create adapter, start the remote session, persist lifecycle."""
        return await self._start_request(
            conversation_id=conversation_id,
            owner_id=owner_id,
            configuration=configuration,
            adapter_version=adapter_version,
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
        adapter_version: str = "0",
        worker_id: str | None = None,
        fence: int | None = None,
    ) -> HarnessSession:
        return await self._start_request(
            conversation_id=conversation_id,
            owner_id=owner_id,
            configuration=configuration,
            adapter_version=adapter_version,
            resume_native_id=native_session_id,
            worker_id=worker_id,
            fence=fence,
        )

    async def prepare_launch_snapshot(
        self,
        configuration: HarnessConfiguration,
        *,
        adapter_version: str = "0",
    ) -> LaunchSnapshot:
        """Probe and build a prospective launch snapshot without mutating bindings."""
        plan = self._plan_launch(configuration=configuration)
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
        adapter_version: str = "0",
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
                    adapter_version=adapter_version,
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
        adapter_version: str,
    ) -> tuple[ManagedRuntime, RecoveryReasonCode]:
        state = await self._persistence.get_worker_snapshot(conversation_id)
        if state.binding is None:
            raise DomainError(ErrorCode.INVALID_STATE, "conversation has no binding")
        binding = state.binding
        plan = self._plan_launch(configuration=configuration)
        _import_remote_seen(
            plan.adapter,
            state.seen_native_ids,
            state.seen_stream_offsets,
        )
        try:
            launch = await self._probe_and_build_launch(
                plan,
                configuration=configuration,
                adapter_version=adapter_version,
            )
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

        handle: SupervisedProcess | None = None
        try:
            process_record = process_record.model_copy(
                update={
                    "status": ProcessStatus.RUNNING,
                    "pid": None,
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
                session = await _await_start_resume(
                    plan.adapter,
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

            if handle is None:
                remote_handle = _remote_process_handle(plan.adapter)
                if remote_handle is not None:
                    handle = remote_handle
                    process_record = process_record.model_copy(update={"pid": remote_handle.pid})

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
            await self._rollback_adapter_startup(
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
        adapter_version: str,
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
                    adapter_version=adapter_version,
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
        adapter_version: str,
        resume_native_id: str | None,
        worker_id: str | None,
        fence: int | None,
    ) -> HarnessSession:
        state = await self._persistence.get_snapshot(conversation_id, owner_id)
        if state.binding is None:
            raise DomainError(ErrorCode.INVALID_STATE, "conversation has no binding")

        binding = state.binding
        plan = self._plan_launch(configuration=configuration)
        adapter = plan.adapter
        _import_remote_seen(adapter, state.seen_native_ids, state.seen_stream_offsets)

        process_id = uuid4()
        process_record = ProcessRecord(
            id=process_id,
            conversation_id=conversation_id,
            binding_id=binding.id,
            status=ProcessStatus.STARTING,
        )
        # Persist STARTING before the remote session create so a crash between
        # the split-side spawn and the RUNNING commit still leaves a record.
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

        handle: SupervisedProcess | None = None
        launch: LaunchSnapshot | None = None
        try:
            launch = await self._probe_and_build_launch(
                plan,
                configuration=configuration,
                adapter_version=adapter_version,
            )
            process_record = process_record.model_copy(
                update={
                    "status": ProcessStatus.RUNNING,
                    "pid": None,
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

            # The split performs its own preflight, spawn, and startup retry
            # inside session create.
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
            session = await _await_start_resume(
                adapter,
                operation,
                timeout=self._policy.start_resume_timeout,
            )

            if handle is None:
                remote_handle = _remote_process_handle(adapter)
                if remote_handle is not None:
                    # The split spawned the process during session create; adopt
                    # its supervised mirror and record the containerized pid.
                    handle = remote_handle
                    process_record = process_record.model_copy(update={"pid": remote_handle.pid})

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
            await asyncio.shield(
                self._rollback_adapter_startup(
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
            raise
        except TimeoutError as exc:
            await self._rollback_adapter_startup(
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
            await self._rollback_adapter_startup(
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
            await self._rollback_adapter_startup(
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

    async def _rollback_adapter_startup(
        self,
        adapter: HarnessAdapter,
        *,
        conversation_id: UUID,
        binding_id: UUID,
        configuration: HarnessConfiguration,
    ) -> None:
        """Close any adapter-side session state left over from a failed start."""
        provisional = HarnessSession(
            conversation_id=conversation_id,
            binding_id=binding_id,
            kind=configuration.kind,
            model=configuration.model,
            mode=configuration.mode,
            effort=configuration.effort,
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
        handle: SupervisedProcess | None,
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
    ) -> _LaunchPlan:
        """Create the adapter for one runtime; splits own executables and spawn."""
        adapter = self._registry.create(configuration.kind)
        set_redaction_patterns = getattr(adapter, "set_redaction_patterns", None)
        if callable(set_redaction_patterns):
            set_redaction_patterns(self._redaction_patterns)
        return _LaunchPlan(adapter=adapter)

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
        launch_getter = getattr(plan.adapter, "last_probe_launch", None)
        if callable(launch_getter):
            remote_launch = launch_getter()
            if isinstance(remote_launch, LaunchSnapshot):
                # Remote adapters return the split-resolved snapshot (container
                # paths, split-side executable resolution).
                return remote_launch
        return self._build_local_launch_snapshot(
            working_directory=configuration.working_directory,
            workspace_roots=configuration.workspace_roots,
            capabilities=caps,
            model=configuration.model,
            mode=configuration.mode,
            adapter_version=adapter_version,
            effort=configuration.effort,
        )

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
        adapter_version: str = "0",
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

        plan = self._plan_launch(configuration=configuration)
        process_id = uuid4()
        handle: SupervisedProcess | None = None
        try:
            launch = await self._probe_and_build_launch(
                plan,
                configuration=configuration,
                adapter_version=adapter_version,
            )
            # Candidates always create a new native session; never resume.
            session = await _await_start_resume(
                plan.adapter,
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

        if handle is None:
            handle = _remote_process_handle(plan.adapter)

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
        managed = self.get_runtime(conversation_id)
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
        handle: SupervisedProcess | None,
        *,
        conversation_id: UUID,
        binding_id: UUID,
        configuration: HarnessConfiguration,
    ) -> None:
        await self._rollback_adapter_startup(
            plan.adapter,
            conversation_id=conversation_id,
            binding_id=binding_id,
            configuration=configuration,
        )
        if handle is not None:
            with contextlib.suppress(Exception):
                await handle.force_terminate(reason="candidate_startup_failure")

    def _build_local_launch_snapshot(
        self,
        *,
        working_directory: str,
        workspace_roots: tuple[str, ...],
        capabilities: HarnessCapabilities,
        model: str | None,
        mode: str | None,
        adapter_version: str,
        effort: str | None = None,
    ) -> LaunchSnapshot:
        """Resolve cwd/roots locally for adapters without a probe snapshot."""
        workdir = resolve_directory(
            working_directory,
            error_code=ErrorCode.WORKING_DIRECTORY_NOT_FOUND,
        )
        roots = tuple(
            resolve_directory(root, error_code=ErrorCode.WORKSPACE_ROOT_NOT_FOUND)
            for root in workspace_roots
        )
        return LaunchSnapshot(
            resolved_executable=None,
            harness_version=capabilities.version,
            working_directory=str(workdir),
            workspace_roots=tuple(str(r) for r in roots),
            model=model,
            mode=mode,
            effort=effort,
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
        if managed.closing or managed.closed:
            return
        managed.closing = True
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

            # Reserve the reap before closing resources. A prompt committed after
            # the snapshot makes this write conflict and leaves the runtime live.
            result = reap_session(state, now=self._clock(), reason="idle")
            try:
                await self._persistence.commit_runtime_lifecycle(
                    conversation_id,
                    state.conversation.version,
                    result.state,
                    None,
                    None,
                    result.events,
                    worker_id=managed.worker_id,
                    fence=managed.fence,
                )
            except DomainError as exc:
                if exc.code is ErrorCode.OPTIMISTIC_CONFLICT:
                    return False
                raise
            get_observability().observe_committed_events(result.events, state=result.state)
            managed.closing = True

            # Close live resources; preserve native resume ID and launch history.
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    managed.adapter.close(managed.session),
                    timeout=self._policy.graceful_close_timeout,
                )
            if managed.process is not None:
                await managed.process.close()
            await self._persist_terminal(managed, reason="idle")
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

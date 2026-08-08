"""RuntimeManager — one supervised runtime per conversation."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
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
from talktoharnesses.runtime.policy import RuntimePolicy
from talktoharnesses.runtime.spec import ProcessSpec
from talktoharnesses.runtime.supervisor import ProcessSupervisor

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _empty_tasks() -> list[asyncio.Task[None]]:
    return []


@dataclass
class ManagedRuntime:
    conversation_id: UUID
    owner_id: str
    adapter: HarnessAdapter
    session: HarnessSession
    process: ProcessHandle
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
        exe = executable_path or configuration.executable_path
        if not exe:
            raise DomainError(
                ErrorCode.INVALID_EXECUTABLE,
                "configuration has no executable_path",
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
                argv=argv,
            )

            handle = await self._supervisor.spawn(
                spec,
                redaction_patterns=self._redaction_patterns,
            )

            process_record = process_record.model_copy(
                update={
                    "status": ProcessStatus.RUNNING,
                    "pid": handle.pid,
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

            if resume_native_id is None:
                session = await asyncio.wait_for(
                    adapter.start(
                        StartSessionRequest(
                            conversation_id=conversation_id,
                            binding_id=binding.id,
                            configuration=configuration,
                            launch=launch,
                        )
                    ),
                    timeout=self._policy.start_resume_timeout,
                )
                result = start_session(
                    state,
                    now=self._clock(),
                    native_session_id=session.native_session_id,
                    launch=launch,
                )
            else:
                session = await asyncio.wait_for(
                    adapter.resume(
                        ResumeSessionRequest(
                            conversation_id=conversation_id,
                            binding_id=binding.id,
                            configuration=configuration,
                            native_session_id=resume_native_id,
                            launch=launch,
                        )
                    ),
                    timeout=self._policy.start_resume_timeout,
                )
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
            pump = asyncio.create_task(
                self._lifecycle_pump(managed),
                name=f"lifecycle-{conversation_id}",
            )
            managed.tasks.append(pump)
            self._runtimes[conversation_id] = managed
            self._arm_idle_timer(conversation_id)
            return session

        except asyncio.CancelledError:
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
            raise
        except TimeoutError as exc:
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

    async def _lifecycle_pump(
        self,
        managed: ManagedRuntime,
    ) -> None:
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
        payloads: list[EventPayload]
        if isinstance(event, ProcessStderrTruncatedEvent):
            process = process.model_copy(
                update={"redacted_stderr_tail": managed.process.redacted_stderr_tail}
            )
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
            process = process.model_copy(
                update={
                    "status": (
                        ProcessStatus.EXITED if event.exit_code == 0 else ProcessStatus.FAILED
                    ),
                    "exit_code": event.exit_code,
                    "exited_at": now,
                    "redacted_stderr_tail": managed.process.redacted_stderr_tail,
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
            process = process.model_copy(
                update={
                    "status": ProcessStatus.TERMINATED,
                    "exited_at": now,
                    "exit_code": managed.process.returncode,
                    "redacted_stderr_tail": managed.process.redacted_stderr_tail,
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
            await managed.process.force_terminate(reason="graceful_close_timeout")
        else:
            await managed.process.close()
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
        event: ProcessEvent
        if managed.process.forced:
            event = ProcessForcedTerminationEvent(
                process_id=managed.process_record.id,
                reason=managed.process.forced_reason or reason,
            )
        else:
            event = ProcessExitedEvent(
                process_id=managed.process_record.id,
                exit_code=managed.process.returncode,
            )

        while True:
            state = await self._persistence.get_snapshot(
                managed.conversation_id,
                managed.owner_id,
            )
            expected_version = state.conversation.version
            prior_events: tuple[ConversationEvent, ...] = ()
            if managed.process.stderr_truncated and not managed.stderr_truncation_persisted:
                state, _, prior_events = self._apply_process_event(
                    managed,
                    state,
                    ProcessStderrTruncatedEvent(
                        process_id=managed.process_record.id,
                        retained_bytes=managed.process.retained_stderr_bytes,
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
                with contextlib.suppress(Exception):
                    await managed.process.force_terminate(reason="shutdown")
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

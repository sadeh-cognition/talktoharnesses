"""ProcessHandle — supervised process without exposing the platform object."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
import time
from collections.abc import AsyncIterator, Callable
from typing import Any
from uuid import UUID

from talktoharnesses.application.redaction import StreamingTextRedactor
from talktoharnesses.runtime.events import (
    ProcessEvent,
    ProcessExitedEvent,
    ProcessForcedTerminationEvent,
    ProcessSilenceWarningEvent,
    ProcessStartedEvent,
    ProcessStderrTruncatedEvent,
)
from talktoharnesses.runtime.policy import RuntimePolicy

STDERR_RETENTION_BYTES = 10 * 1024 * 1024


class ProcessHandle:
    """Async handle over a supervised child process.

    Stdout is an opaque single-consumer byte stream. Stderr is redacted and
    retained (newest 10 MiB of valid UTF-8). Lifecycle events never carry
    stdout bytes.
    """

    def __init__(
        self,
        *,
        process_id: UUID,
        process: asyncio.subprocess.Process,
        policy: RuntimePolicy,
        redactor: StreamingTextRedactor,
        job: Any | None = None,
        on_lifecycle: Callable[[ProcessEvent], None] | None = None,
    ) -> None:
        self.process_id = process_id
        self._process = process
        self._policy = policy
        self._redactor = redactor
        self._job = job
        self._on_lifecycle = on_lifecycle

        self._stderr_tail = ""
        self._stderr_truncated = False
        self._stdout_consumer_taken = False
        self._events_consumer_taken = False
        self._closed = False
        self._forced = False
        self._exit_code: int | None = None
        self._exit_event = asyncio.Event()
        self._last_stdout_at = time.monotonic()
        self._silence_episode_active = False

        self._event_q: asyncio.Queue[ProcessEvent | None] = asyncio.Queue()
        self._tasks: list[asyncio.Task[None]] = []
        self._stderr_task: asyncio.Task[None]

        self._emit(ProcessStartedEvent(process_id=process_id, pid=process.pid))

        self._stderr_task = asyncio.create_task(self._read_stderr(), name="stderr-reader")
        self._tasks.append(self._stderr_task)
        self._tasks.append(asyncio.create_task(self._watch_exit(), name="exit-watcher"))
        self._tasks.append(asyncio.create_task(self._silence_watch(), name="silence-watch"))

    @property
    def pid(self) -> int | None:
        return self._process.pid

    @property
    def redacted_stderr_tail(self) -> str:
        return self._stderr_tail

    @property
    def returncode(self) -> int | None:
        return self._process.returncode

    @property
    def forced(self) -> bool:
        return self._forced

    @property
    def forced_reason(self) -> str | None:
        return getattr(self, "_forced_reason", None)

    @property
    def stderr_truncated(self) -> bool:
        return self._stderr_truncated

    @property
    def retained_stderr_bytes(self) -> int:
        return len(self._stderr_tail.encode("utf-8"))

    def _emit(self, event: ProcessEvent) -> None:
        self._event_q.put_nowait(event)
        if self._on_lifecycle is not None:
            self._on_lifecycle(event)

    async def write_stdin(self, data: bytes) -> None:
        if self._process.stdin is None:
            msg = "stdin is not available"
            raise RuntimeError(msg)
        self._process.stdin.write(data)
        await self._process.stdin.drain()

    def stdout(self) -> AsyncIterator[bytes]:
        if self._stdout_consumer_taken:
            msg = "stdout stream allows a single consumer"
            raise RuntimeError(msg)
        self._stdout_consumer_taken = True
        return self._stdout_iter()

    async def _stdout_iter(self) -> AsyncIterator[bytes]:
        assert self._process.stdout is not None
        while chunk := await self._process.stdout.read(65536):
            self._last_stdout_at = time.monotonic()
            self._silence_episode_active = False
            yield chunk

    def events(self) -> AsyncIterator[ProcessEvent]:
        if self._events_consumer_taken:
            msg = "events stream allows a single consumer"
            raise RuntimeError(msg)
        self._events_consumer_taken = True
        return self._events_iter()

    async def _events_iter(self) -> AsyncIterator[ProcessEvent]:
        while True:
            event = await self._event_q.get()
            if event is None:
                return
            yield event

    async def wait(self) -> int | None:
        await self._exit_event.wait()
        return self._exit_code

    async def interrupt(self) -> None:
        """Send a graceful interrupt (SIGINT / CTRL_BREAK) to the process group."""
        if self._process.returncode is not None:
            return
        await self._signal_group(graceful=True)

    async def close(self) -> None:
        """Graceful close: interrupt, wait up to graceful_close_timeout, then escalate."""
        if self._closed:
            return
        self._closed = True
        if self._process.returncode is None:
            await self._signal_group(graceful=True)
            try:
                await asyncio.wait_for(
                    self._process.wait(),
                    timeout=self._policy.graceful_close_timeout,
                )
            except TimeoutError:
                await self.force_terminate(reason="graceful_close_timeout")
                return
        await self.wait()
        await self._finalize()

    async def force_terminate(self, *, reason: str | None = "forced") -> None:
        """Escalate to tree termination (process group / Job Object)."""
        if self._closed and self._exit_event.is_set():
            return
        already_exited = self._process.returncode is not None
        self._forced = not already_exited
        self._forced_reason = reason
        self._closed = True
        if not already_exited:
            await self._kill_tree()
            try:
                await asyncio.wait_for(
                    self._process.wait(),
                    timeout=self._policy.terminate_escalation,
                )
            except TimeoutError:
                await self._kill_tree(hard=True)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._process.wait(), timeout=1.0)
        await self.wait()
        await self._finalize()

    async def _finalize(self) -> None:
        current = asyncio.current_task()
        pending = [task for task in self._tasks if task is not current and not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._tasks.clear()
        if self._process.stdin is not None:
            with contextlib.suppress(Exception):
                self._process.stdin.close()
        if self._process.returncode is None:
            with contextlib.suppress(ProcessLookupError, OSError):
                self._process.kill()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._process.wait(), timeout=1.0)
        if self._job is not None:
            from talktoharnesses.runtime.windows_job import close_job

            close_job(self._job)
            self._job = None
        # Unblock consumers.
        with contextlib.suppress(asyncio.QueueFull):
            self._event_q.put_nowait(None)

    async def _read_stderr(self) -> None:
        assert self._process.stderr is not None
        try:
            while True:
                raw = await self._process.stderr.read(65536)
                if not raw:
                    break
                text = raw.decode("utf-8", errors="replace")
                redacted = self._redactor.feed(text)
                if redacted:
                    self._append_stderr(redacted)
        finally:
            tail = self._redactor.flush()
            if tail:
                self._append_stderr(tail)

    def _append_stderr(self, text: str) -> None:
        combined = self._stderr_tail + text
        encoded = combined.encode("utf-8")
        if len(encoded) > STDERR_RETENTION_BYTES:
            # Keep newest bytes; re-decode as valid UTF-8.
            encoded = encoded[-STDERR_RETENTION_BYTES:]
            combined = encoded.decode("utf-8", errors="ignore")
            if not self._stderr_truncated:
                self._stderr_truncated = True
                retained = len(combined.encode("utf-8"))
                self._emit(
                    ProcessStderrTruncatedEvent(
                        process_id=self.process_id,
                        retained_bytes=retained,
                    )
                )
        self._stderr_tail = combined

    async def _watch_exit(self) -> None:
        code = await self._process.wait()
        # Stderr is part of the terminal process checkpoint. Drain it, including
        # the streaming redactor carry, before publishing the terminal event.
        await self._stderr_task
        self._exit_code = code
        if self._forced:
            self._emit(
                ProcessForcedTerminationEvent(
                    process_id=self.process_id,
                    reason=getattr(self, "_forced_reason", None),
                )
            )
        else:
            self._emit(ProcessExitedEvent(process_id=self.process_id, exit_code=code))
        self._exit_event.set()
        self._event_q.put_nowait(None)

    async def _silence_watch(self) -> None:
        interval = min(0.05, self._policy.silence_warning / 4)
        try:
            while not self._exit_event.is_set():
                await asyncio.sleep(interval)
                if self._exit_event.is_set():
                    return
                idle = time.monotonic() - self._last_stdout_at
                if idle >= self._policy.silence_warning and not self._silence_episode_active:
                    self._silence_episode_active = True
                    self._emit(ProcessSilenceWarningEvent(process_id=self.process_id))
        except asyncio.CancelledError:
            return

    async def _signal_group(self, *, graceful: bool) -> None:
        if self._process.returncode is not None:
            return
        pid = self._process.pid
        if sys.platform == "win32":
            # CREATE_NEW_PROCESS_GROUP: CTRL_BREAK_EVENT is the portable interrupt.
            ctrl_break = getattr(signal, "CTRL_BREAK_EVENT", None)
            if ctrl_break is not None:
                with contextlib.suppress(ProcessLookupError, OSError):
                    self._process.send_signal(ctrl_break)
            return
        sig = signal.SIGINT if graceful else signal.SIGTERM
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(pid, sig)

    async def _kill_tree(self, *, hard: bool = False) -> None:
        if self._process.returncode is not None:
            return
        pid = self._process.pid
        if sys.platform == "win32":
            if self._job is not None:
                from talktoharnesses.runtime.windows_job import terminate_job

                terminate_job(self._job, 1)
            with contextlib.suppress(ProcessLookupError, OSError):
                self._process.kill()
            return
        # Unix: SIGTERM then SIGKILL to the process group.
        sig = signal.SIGKILL if hard else signal.SIGTERM
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(pid, sig)
        if not hard:
            try:
                await asyncio.wait_for(
                    self._process.wait(),
                    timeout=self._policy.terminate_escalation,
                )
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                    os.killpg(pid, signal.SIGKILL)

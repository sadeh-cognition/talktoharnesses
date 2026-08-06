"""asyncio subprocess spawn / lifecycle / teardown."""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from talktoharnesses.errors import ProcessError

__all__ = [
    "DEFAULT_STDERR_TAIL_BYTES",
    "DEFAULT_TERMINATE_TIMEOUT",
    "ManagedProcess",
    "spawn_process",
]

DEFAULT_TERMINATE_TIMEOUT = 5.0

#: Bytes of child stderr retained for diagnostics. Anything past this is
#: discarded oldest-first — we only ever want the tail for an error message.
DEFAULT_STDERR_TAIL_BYTES = 64 * 1024


@dataclass
class ManagedProcess:
    """Thin wrapper around an asyncio subprocess with reliable teardown."""

    process: asyncio.subprocess.Process
    command: list[str]
    cwd: Path | None = None
    stderr_tail_bytes: int = DEFAULT_STDERR_TAIL_BYTES
    _terminated: bool = field(default=False, init=False, repr=False)
    _stderr_buf: bytearray = field(default_factory=bytearray, init=False, repr=False)
    _stderr_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)

    @property
    def pid(self) -> int | None:
        return self.process.pid

    @property
    def returncode(self) -> int | None:
        return self.process.returncode

    @property
    def stdin(self) -> asyncio.StreamWriter | None:
        return self.process.stdin

    @property
    def stdout(self) -> asyncio.StreamReader | None:
        return self.process.stdout

    @property
    def stderr(self) -> asyncio.StreamReader | None:
        return self.process.stderr

    def is_running(self) -> bool:
        return self.process.returncode is None

    async def wait(self) -> int:
        return await self.process.wait()

    # -- stderr -------------------------------------------------------------

    def start_stderr_drain(self) -> None:
        """Continuously read child stderr into a bounded tail buffer.

        Without this the pipe is never read: agent CLIs that log heavily fill
        the OS buffer and block on write, wedging the whole harness. Draining
        also means the tail is available for error messages — a CLI that exits
        because it is not authenticated says so on stderr, and otherwise that
        text is simply lost.
        """
        if self._stderr_task is not None and not self._stderr_task.done():
            return
        if self.process.stderr is None:
            return
        self._stderr_task = asyncio.create_task(
            self._drain_stderr(), name="process-stderr-drain"
        )

    async def _drain_stderr(self) -> None:
        stream = self.process.stderr
        if stream is None:
            return
        try:
            while True:
                chunk = await stream.read(4096)
                if not chunk:
                    break
                self._stderr_buf.extend(chunk)
                excess = len(self._stderr_buf) - self.stderr_tail_bytes
                if excess > 0:
                    del self._stderr_buf[:excess]
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — diagnostics only, never fatal
            return

    def stderr_tail(self, *, max_chars: int = 2000) -> str:
        """Most recent child stderr output, decoded and trimmed."""
        text = self._stderr_buf.decode("utf-8", errors="replace").strip()
        if len(text) > max_chars:
            return "…" + text[-max_chars:]
        return text

    async def terminate(
        self,
        *,
        timeout: float = DEFAULT_TERMINATE_TIMEOUT,
    ) -> int:
        """SIGTERM, then SIGKILL after ``timeout`` if still running."""
        if self._terminated and self.process.returncode is not None:
            return self.process.returncode
        self._terminated = True

        if self.process.returncode is not None:
            return self.process.returncode

        try:
            self.process.terminate()
        except ProcessLookupError:
            return await self.process.wait()

        try:
            return await asyncio.wait_for(self.process.wait(), timeout=timeout)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                self.process.kill()
            return await self.process.wait()

    async def aclose(self, *, timeout: float = DEFAULT_TERMINATE_TIMEOUT) -> int:
        """Close stdin if open, wait for exit, then force-terminate if needed."""
        if self.process.stdin is not None and not self.process.stdin.is_closing():
            try:
                self.process.stdin.close()
                await self.process.stdin.wait_closed()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
        try:
            if self.process.returncode is not None:
                return self.process.returncode
            try:
                return await asyncio.wait_for(self.process.wait(), timeout=timeout)
            except TimeoutError:
                return await self.terminate(timeout=timeout)
        finally:
            await self._stop_stderr_drain()

    async def _stop_stderr_drain(self) -> None:
        task = self._stderr_task
        if task is None:
            return
        self._stderr_task = None
        if task.done():
            return
        # The child has exited by now, so the drain should see EOF promptly.
        # Cancel if it does not, to avoid hanging teardown on a stuck pipe.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), timeout=1.0)
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


async def spawn_process(
    command: Sequence[str],
    *,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    stdin: int | None = asyncio.subprocess.PIPE,
    stdout: int | None = asyncio.subprocess.PIPE,
    stderr: int | None = asyncio.subprocess.PIPE,
    **kwargs: Any,
) -> ManagedProcess:
    """Spawn a subprocess. Raises ``ProcessError`` if the binary cannot start."""
    cmd = [str(c) for c in command]
    if not cmd:
        raise ProcessError("command must be non-empty")

    merged_env: dict[str, str] | None = None
    if env is not None:
        merged_env = {**os.environ, **dict(env)}

    workdir = str(cwd) if cwd is not None else None

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=workdir,
            env=merged_env,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            **kwargs,
        )
    except FileNotFoundError as exc:
        raise ProcessError(
            f"Failed to spawn {cmd[0]!r}: executable not found",
        ) from exc
    except OSError as exc:
        raise ProcessError(f"Failed to spawn {cmd!r}: {exc}") from exc

    managed = ManagedProcess(
        process=proc,
        command=cmd,
        cwd=Path(workdir) if workdir else None,
    )
    # Always drain stderr: an unread pipe eventually blocks the child, and the
    # tail is what explains a failed startup.
    managed.start_stderr_drain()
    return managed

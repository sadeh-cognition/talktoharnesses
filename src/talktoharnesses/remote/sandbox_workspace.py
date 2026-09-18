"""Repo-declared workspace setup, run by the proxy inside a kind's sandbox.

A working directory can carry ``.tth/setup.sh`` (installing its Python
environment, ``node_modules`` and so on). Before a split session starts
there, the proxy runs that script inside the kind's running container as the
service user through ``docker exec``: the container already has the project
bind mounts, the toolchain caches on ``/data`` and the session's resource
limits, so no second container or API field is needed. The script is the
repository's; TTH decides only when it runs (stamp on ``/data``), how long it
may take, and how its outcome is reported.

The in-container half is :mod:`talktoharnesses.remote.workspace_runner`,
shipped as source on the exec command line.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from tth_types.enums import ErrorCode, HarnessKind
from tth_types.errors import DomainError

from talktoharnesses.application.redaction import StreamingTextRedactor
from talktoharnesses.remote import workspace_runner

logger = logging.getLogger(__name__)

SETUP_FILE = workspace_runner.SETUP_FILE
# Per-kind ``/data`` volume; one state directory per working directory.
STATE_ROOT = "/data/tth/workspaces"
OUTPUT_TAIL_BYTES = 4096
# The sandbox base interpreter; never the service venv.
_RUNNER_PYTHON = "/usr/local/bin/python3"
_RUNNER_SOURCE = Path(workspace_runner.__file__).read_text(encoding="utf-8")

# Toolchain caches and download locations for agents (and setup scripts)
# working in mounted projects. Injected into every sandbox container by the
# sandbox manager so they land on the persistent /data volume; the image
# itself exports none of them.
TOOLCHAIN_ENV: dict[str, str] = {
    "UV_CACHE_DIR": "/data/uv/cache",
    "UV_PYTHON_INSTALL_DIR": "/data/uv/python",
    # Bind-mounted projects sit on another filesystem than the cache volume,
    # so hardlinks would fail; copying is the quiet choice.
    "UV_LINK_MODE": "copy",
    "npm_config_cache": "/data/npm/cache",
    "npm_config_update_notifier": "false",
    "COREPACK_HOME": "/data/corepack",
    "COREPACK_ENABLE_DOWNLOAD_PROMPT": "0",
    "npm_config_store_dir": "/data/pnpm/store",
    "YARN_CACHE_FOLDER": "/data/yarn/cache",
}

WorkspaceSetupStatus = Literal["succeeded", "skipped", "absent"]
WorkspaceSetupFailureReason = Literal["exit_status", "timeout", "lock_timeout", "runner_error"]


class WorkspaceSetupFailed(DomainError):
    """``workspace_setup_failed`` with its outcome as typed fields.

    ``details`` carries the same values for the HTTP error body; the
    runtime reads the attributes.
    """

    def __init__(
        self,
        reason: WorkspaceSetupFailureReason,
        *,
        kind: HarnessKind,
        working_directory: str,
        message: str,
        exit_code: int | None = None,
        output_tail: str = "",
    ) -> None:
        super().__init__(
            ErrorCode.WORKSPACE_SETUP_FAILED,
            message,
            details={
                "kind": kind.value,
                "reason": reason,
                "working_directory": working_directory,
                "setup_file": SETUP_FILE,
                "exit_code": exit_code,
                "output_tail": output_tail,
            },
        )
        self.reason: WorkspaceSetupFailureReason = reason
        self.working_directory = working_directory
        self.exit_code = exit_code
        self.output_tail = output_tail

    @property
    def completed_status(self) -> Literal["failed", "timed_out"]:
        return "timed_out" if self.reason == "timeout" else "failed"


@dataclass(frozen=True)
class WorkspaceSetupOutcome:
    status: WorkspaceSetupStatus
    working_directory: str
    setup_file: str = SETUP_FILE
    stamp: str | None = None
    exit_code: int | None = None
    duration_ms: int | None = None
    output_tail: str = ""


@dataclass(frozen=True)
class WorkspaceSetupStarted:
    """Passed to ``on_started`` when the script actually begins running."""

    working_directory: str
    setup_file: str
    stamp: str


def workspace_key(working_directory: str) -> str:
    return hashlib.sha256(working_directory.encode("utf-8")).hexdigest()[:16]


def state_directory(working_directory: str) -> str:
    return f"{STATE_ROOT}/{workspace_key(working_directory)}"


def output_tail(text: str, limit: int = OUTPUT_TAIL_BYTES) -> str:
    """The newest ``limit`` bytes of ``text`` at a valid UTF-8 boundary."""
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    truncated = encoded[-limit:]
    while truncated:
        try:
            return truncated.decode("utf-8")
        except UnicodeDecodeError:
            truncated = truncated[1:]
    return ""


def runner_command(
    *,
    working_directory: str,
    timeout: float,
    image_id: str,
    kind: HarnessKind,
) -> list[str]:
    return [
        _RUNNER_PYTHON,
        "-c",
        _RUNNER_SOURCE,
        "--working-directory",
        working_directory,
        "--state-dir",
        state_directory(working_directory),
        "--timeout",
        str(timeout),
        "--image-id",
        image_id,
        "--kind",
        kind.value,
    ]


def _failure(
    reason: WorkspaceSetupFailureReason,
    *,
    kind: HarnessKind,
    working_directory: str,
    message: str,
    exit_code: int | None = None,
    tail: str = "",
) -> WorkspaceSetupFailed:
    return WorkspaceSetupFailed(
        reason,
        kind=kind,
        working_directory=working_directory,
        message=message,
        exit_code=exit_code,
        output_tail=tail,
    )


class _PhaseReader:
    """Accumulates the runner's stderr and yields complete JSON phase lines."""

    def __init__(self) -> None:
        self._buffer = b""

    def feed(self, chunk: bytes) -> list[dict[str, Any]]:
        self._buffer += chunk
        phases: list[dict[str, Any]] = []
        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            phase = _parse_phase(line)
            if phase is not None:
                phases.append(phase)
        return phases


def _parse_phase(line: bytes) -> dict[str, Any] | None:
    text = line.decode("utf-8", errors="replace").strip()
    if not text:
        return None
    try:
        parsed: object = json.loads(text)
    except ValueError:
        logger.warning("workspace setup runner wrote a non-JSON stderr line: %s", text)
        return None
    if not isinstance(parsed, dict):
        logger.warning("workspace setup runner wrote an unexpected stderr line: %s", text)
        return None
    phase = cast(dict[str, Any], parsed)
    if not isinstance(phase.get("phase"), str):
        logger.warning("workspace setup runner wrote an unexpected stderr line: %s", text)
        return None
    return phase


class _RollingTail:
    """The newest ``OUTPUT_TAIL_BYTES`` of a stream, logged line by line as it arrives.

    Setup output is unbounded (a verbose ``npm install`` runs to megabytes)
    and the proxy only reports a tail; the full log lives in the container
    at ``<state dir>/setup.log``. Nothing here grows with the stream.
    """

    # A character encodes to at least one byte, so this many characters
    # always cover the byte tail with room for one more chunk.
    _KEEP_CHARS = 2 * OUTPUT_TAIL_BYTES

    def __init__(self, working_directory: str) -> None:
        self._working_directory = working_directory
        self._text = ""
        self._line = ""

    def feed(self, text: str) -> None:
        if not text:
            return
        self._text = (self._text + text)[-self._KEEP_CHARS :]
        self._line += text
        *lines, self._line = self._line.split("\n")
        if len(self._line) > self._KEEP_CHARS:
            # A newline-free stream (progress bars) is logged in pieces.
            lines.append(self._line)
            self._line = ""
        for line in lines:
            logger.info("workspace setup output for %s: %s", self._working_directory, line)

    def close(self) -> None:
        if self._line:
            logger.info("workspace setup output for %s: %s", self._working_directory, self._line)
            self._line = ""

    def text(self) -> str:
        return output_tail(self._text)


def run_setup(
    client: Any,
    *,
    container_name: str,
    kind: HarnessKind,
    working_directory: str,
    timeout: float,
    redaction_patterns: tuple[str, ...] = (),
    on_started: Callable[[WorkspaceSetupStarted], None] | None = None,
) -> WorkspaceSetupOutcome:
    """Blocking; run the working directory's setup script in the kind's container.

    Raises ``DomainError(WORKSPACE_SETUP_FAILED)`` when the script exits
    non-zero, times out, cannot take the per-workspace lock, or cannot be
    executed at all.
    """
    from docker.errors import DockerException

    try:
        container = client.containers.get(container_name)
        image_id = str(container.image.id)
        command = runner_command(
            working_directory=working_directory,
            timeout=timeout,
            image_id=image_id,
            kind=kind,
        )
        exec_id = client.api.exec_create(
            container.id, command, user="agent", workdir=working_directory
        )["Id"]
        stream = client.api.exec_start(exec_id, stream=True, demux=True)
        redactor = StreamingTextRedactor(redaction_patterns)
        phases = _PhaseReader()
        tail = _RollingTail(working_directory)
        seen: list[dict[str, Any]] = []
        for stdout_chunk, stderr_chunk in stream:
            if stdout_chunk:
                tail.feed(redactor.feed(stdout_chunk.decode("utf-8", errors="replace")))
            if stderr_chunk:
                for phase in phases.feed(stderr_chunk):
                    seen.append(phase)
                    if phase["phase"] == "start" and on_started is not None:
                        on_started(
                            WorkspaceSetupStarted(
                                working_directory=working_directory,
                                setup_file=str(phase.get("setup_file") or SETUP_FILE),
                                stamp=str(phase.get("stamp") or ""),
                            )
                        )
        tail.feed(redactor.flush())
        tail.close()
        exit_status = client.api.exec_inspect(exec_id).get("ExitCode")
    except DockerException as exc:
        logger.warning(
            "workspace setup could not run in %s for %s: %s", container_name, working_directory, exc
        )
        raise _failure(
            "runner_error",
            kind=kind,
            working_directory=working_directory,
            message=f"workspace setup could not run in {container_name}: {exc}",
        ) from exc

    tail = tail.text()
    final = seen[-1] if seen else None
    final_phase = final["phase"] if final is not None else None
    logger.info(
        "workspace setup in %s for %s: phase=%s exit=%s",
        container_name,
        working_directory,
        final_phase,
        exit_status,
    )

    if final_phase == "absent":
        return WorkspaceSetupOutcome(status="absent", working_directory=working_directory)
    if final_phase == "skip":
        return WorkspaceSetupOutcome(
            status="skipped",
            working_directory=working_directory,
            stamp=_optional_str(final, "stamp"),
        )
    if final_phase == "end":
        exit_code = _optional_int(final, "exit_code")
        duration_ms = _optional_int(final, "duration_ms")
        if exit_code == 0 and exit_status == 0:
            return WorkspaceSetupOutcome(
                status="succeeded",
                working_directory=working_directory,
                stamp=_optional_str(final, "stamp"),
                exit_code=0,
                duration_ms=duration_ms,
                output_tail=tail,
            )
        raise _failure(
            "exit_status",
            kind=kind,
            working_directory=working_directory,
            message=f"{SETUP_FILE} exited with status {exit_code} in {working_directory}",
            exit_code=exit_code,
            tail=tail,
        )
    if final_phase == "timeout":
        raise _failure(
            "timeout",
            kind=kind,
            working_directory=working_directory,
            message=f"{SETUP_FILE} exceeded {timeout:g}s in {working_directory}",
            tail=tail,
        )
    if final_phase == "lock_timeout":
        raise _failure(
            "lock_timeout",
            kind=kind,
            working_directory=working_directory,
            message=f"workspace setup lock for {working_directory} was not released in time",
        )
    detail = _optional_str(final, "message") if final_phase == "error" else None
    raise _failure(
        "runner_error",
        kind=kind,
        working_directory=working_directory,
        message=(
            f"workspace setup runner failed in {working_directory}: "
            f"{detail or f'exit status {exit_status}, last phase {final_phase!r}'}"
        ),
        tail=tail,
    )


def _optional_str(phase: dict[str, Any] | None, key: str) -> str | None:
    value = phase.get(key) if phase is not None else None
    return value if isinstance(value, str) else None


def _optional_int(phase: dict[str, Any] | None, key: str) -> int | None:
    value = phase.get(key) if phase is not None else None
    return value if isinstance(value, int) and not isinstance(value, bool) else None

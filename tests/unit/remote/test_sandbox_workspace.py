"""Proxy side of workspace setup: the docker exec, phase protocol and error mapping."""

from __future__ import annotations

import json
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from tth_types.enums import ErrorCode, HarnessKind
from tth_types.errors import DomainError, public_message

from talktoharnesses.remote import sandbox_workspace, workspace_runner
from talktoharnesses.remote.sandbox_workspace import (
    WorkspaceSetupStarted,
    output_tail,
    run_setup,
    runner_command,
    state_directory,
    workspace_key,
)

Chunk = tuple[bytes | None, bytes | None]


def _phase(**fields: object) -> bytes:
    return (json.dumps(fields) + "\n").encode()


class FakeDocker:
    """docker-py subset: ``containers.get`` plus the low-level exec API."""

    def __init__(self, chunks: list[Chunk], *, exit_code: int = 0) -> None:
        self.chunks = chunks
        self.exit_code = exit_code
        self.exec_create_calls: list[dict[str, Any]] = []
        self.containers = SimpleNamespace(get=self._get)
        self.api = SimpleNamespace(
            exec_create=self._exec_create,
            exec_start=self._exec_start,
            exec_inspect=self._exec_inspect,
        )

    def _get(self, name: str) -> Any:
        return SimpleNamespace(id=f"container-{name}", image=SimpleNamespace(id="sha256:img"))

    def _exec_create(self, container_id: str, cmd: list[str], **kwargs: Any) -> dict[str, str]:
        self.exec_create_calls.append({"container_id": container_id, "cmd": cmd, **kwargs})
        return {"Id": "exec-1"}

    def _exec_start(self, exec_id: str, **kwargs: Any) -> Iterator[Chunk]:
        assert kwargs == {"stream": True, "demux": True}
        return iter(self.chunks)

    def _exec_inspect(self, exec_id: str) -> dict[str, Any]:
        return {"ExitCode": self.exit_code}


def _run(client: FakeDocker, **kwargs: Any) -> Any:
    return run_setup(
        client,
        container_name="tth-codex",
        kind=HarnessKind.CODEX,
        working_directory="/home/dev/project",
        timeout=300.0,
        **kwargs,
    )


def test_runner_command_ships_the_runner_source_to_the_base_interpreter() -> None:
    command = runner_command(
        working_directory="/home/dev/project",
        timeout=300.0,
        image_id="sha256:img",
        kind=HarnessKind.GROK,
    )

    assert command[:2] == ["/usr/local/bin/python3", "-c"]
    assert "def main(" in command[2]
    assert "/opt/tth/venv" not in command[0]
    assert command[3:] == [
        "--working-directory",
        "/home/dev/project",
        "--state-dir",
        state_directory("/home/dev/project"),
        "--timeout",
        "300.0",
        "--image-id",
        "sha256:img",
        "--kind",
        "grok",
    ]


def test_state_directory_is_per_working_directory_on_the_data_volume() -> None:
    assert state_directory("/a").startswith("/data/tth/workspaces/")
    assert workspace_key("/a") != workspace_key("/b")
    assert len(workspace_key("/a")) == 16


def test_output_tail_keeps_the_newest_bytes_at_a_utf8_boundary() -> None:
    text = "é" * 3000
    tail = output_tail(text, limit=7)
    assert tail == "é" * 3
    assert output_tail("short", limit=7) == "short"


def test_successful_run_returns_outcome_and_fires_on_started() -> None:
    client = FakeDocker(
        [
            (None, _phase(phase="start", stamp="abc", setup_file=".tth/setup.sh")),
            (b"installing\n", None),
            (b"done\n", _phase(phase="end", exit_code=0, duration_ms=1234, stamp="def")),
        ]
    )
    started: list[WorkspaceSetupStarted] = []

    outcome = _run(client, on_started=started.append)

    assert outcome.status == "succeeded"
    assert outcome.exit_code == 0
    assert outcome.duration_ms == 1234
    assert outcome.stamp == "def"
    assert outcome.output_tail == "installing\ndone\n"
    assert started == [
        WorkspaceSetupStarted(
            working_directory="/home/dev/project", setup_file=".tth/setup.sh", stamp="abc"
        )
    ]
    call = client.exec_create_calls[0]
    assert call["container_id"] == "container-tth-codex"
    assert call["user"] == "agent"
    assert call["workdir"] == "/home/dev/project"
    assert call["cmd"][0] == "/usr/local/bin/python3"


def test_absent_and_skip_phases_produce_quiet_outcomes() -> None:
    absent = _run(FakeDocker([(None, _phase(phase="absent", setup_file=".tth/setup.sh"))]))
    assert absent.status == "absent"

    skipped = _run(FakeDocker([(None, _phase(phase="skip", stamp="abc"))]))
    assert skipped.status == "skipped"
    assert skipped.stamp == "abc"


def test_output_is_redacted_and_tail_bounded() -> None:
    client = FakeDocker(
        [
            (None, _phase(phase="start", stamp="abc", setup_file=".tth/setup.sh")),
            (b"token=sk-sec", None),
            (b"ret-value\n" + b"x" * 5000, None),
            (None, _phase(phase="end", exit_code=0, duration_ms=1, stamp="abc")),
        ]
    )

    outcome = _run(client, redaction_patterns=("sk-secret-value",))

    assert "sk-secret-value" not in outcome.output_tail
    assert len(outcome.output_tail.encode()) <= sandbox_workspace.OUTPUT_TAIL_BYTES
    assert outcome.output_tail.endswith("x" * 100)


def test_script_failure_maps_to_exit_status_reason_with_tail() -> None:
    client = FakeDocker(
        [
            (None, _phase(phase="start", stamp="abc", setup_file=".tth/setup.sh")),
            (b"npm ERR! boom\n", None),
            (None, _phase(phase="end", exit_code=7, duration_ms=5)),
        ],
        exit_code=workspace_runner.EXIT_SCRIPT_FAILED,
    )

    with pytest.raises(DomainError) as excinfo:
        _run(client)

    error = excinfo.value
    assert error.code is ErrorCode.WORKSPACE_SETUP_FAILED
    assert error.details["reason"] == "exit_status"
    assert error.details["exit_code"] == 7
    assert error.details["output_tail"] == "npm ERR! boom\n"
    assert error.details["setup_file"] == ".tth/setup.sh"
    assert public_message(error.code, details=error.details) == (
        "workspace setup script (.tth/setup.sh) exited with an error"
    )


@pytest.mark.parametrize(
    ("phases", "exit_code", "reason"),
    [
        ([_phase(phase="start", stamp="a", setup_file="s"), _phase(phase="timeout")], 2, "timeout"),
        ([_phase(phase="lock_timeout")], 4, "lock_timeout"),
        ([_phase(phase="error", message="OSError: boom")], 3, "runner_error"),
        ([], 137, "runner_error"),
    ],
    ids=["timeout", "lock-timeout", "runner-error", "no-phases"],
)
def test_other_failures_map_to_their_reasons(
    phases: list[bytes], exit_code: int, reason: str
) -> None:
    client = FakeDocker([(None, line) for line in phases], exit_code=exit_code)

    with pytest.raises(DomainError) as excinfo:
        _run(client)

    assert excinfo.value.code is ErrorCode.WORKSPACE_SETUP_FAILED
    assert excinfo.value.details["reason"] == reason
    assert public_message(excinfo.value.code, details=excinfo.value.details) != "conflict"


def test_docker_errors_map_to_runner_error() -> None:
    from docker.errors import DockerException

    class Broken(FakeDocker):
        def _get(self, name: str) -> Any:
            raise DockerException("daemon gone")

    with pytest.raises(DomainError) as excinfo:
        _run(Broken([]))

    assert excinfo.value.details["reason"] == "runner_error"


def test_non_json_stderr_lines_are_ignored() -> None:
    client = FakeDocker(
        [
            (None, b"Traceback noise\n"),
            (None, b'{"not": "a phase"}\n'),
            (None, _phase(phase="absent", setup_file=".tth/setup.sh")),
        ]
    )

    assert _run(client).status == "absent"


def _large_stream(chunk: bytes, total_chunks: int) -> Iterator[Chunk]:
    yield (None, _phase(phase="start", stamp="abc", setup_file=".tth/setup.sh"))
    for index in range(total_chunks):
        yield (chunk if index < total_chunks - 1 else chunk + b"final-marker\n", None)
    yield (None, _phase(phase="end", exit_code=0, duration_ms=1, stamp="abc"))


def test_output_tail_is_bounded_while_streaming() -> None:
    """A multi-megabyte stream never accumulates in the proxy; only the tail survives."""
    import logging
    import tracemalloc

    chunk = (b"line " * 13 + b"\n") * 1000  # 66 KB of newline-terminated lines
    total_chunks = 64  # ~4 MiB streamed
    client = FakeDocker([])

    def exec_start(exec_id: str, **kwargs: Any) -> Iterator[Chunk]:
        return _large_stream(chunk, total_chunks)

    client.api.exec_start = exec_start

    logging.disable(logging.INFO)  # measure the code, not the captured log records
    try:
        tracemalloc.start()
        outcome = _run(client)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    finally:
        logging.disable(logging.NOTSET)

    assert outcome.status == "succeeded"
    assert outcome.output_tail.endswith("final-marker\n")
    assert len(outcome.output_tail.encode()) <= sandbox_workspace.OUTPUT_TAIL_BYTES
    assert peak < 1024 * 1024


def test_output_is_logged_line_by_line_as_it_streams(caplog: pytest.LogCaptureFixture) -> None:
    client = FakeDocker(
        [
            (None, _phase(phase="start", stamp="abc", setup_file=".tth/setup.sh")),
            (b"one\ntw", None),
            (b"o\nthree", None),
            (None, _phase(phase="end", exit_code=0, duration_ms=1, stamp="abc")),
        ]
    )

    with caplog.at_level("INFO", logger="talktoharnesses.remote.sandbox_workspace"):
        outcome = _run(client)

    assert outcome.output_tail == "one\ntwo\nthree"
    lines = [
        r.getMessage().rsplit(": ", 1)[1]
        for r in caplog.records
        if "workspace setup output" in r.getMessage()
    ]
    assert lines == ["one", "two", "three"]


def test_runner_module_imports_without_fcntl(monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows hosts import the proxy package; the Unix lock import is deferred."""
    import importlib
    import sys

    assert "\nimport fcntl\n" not in sandbox_workspace._RUNNER_SOURCE  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setitem(sys.modules, "fcntl", None)
    importlib.reload(workspace_runner)
    assert workspace_runner.SETUP_FILE == ".tth/setup.sh"

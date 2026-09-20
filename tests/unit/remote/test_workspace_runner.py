"""The in-container workspace setup runner, exercised on the host with bash."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from talktoharnesses.remote import workspace_runner

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="bash and flock")

_RUNNER = Path(workspace_runner.__file__)
Result = tuple[int, str, list[dict[str, object]]]


def _argv(repo: Path, state: Path, *, timeout: float = 10.0, image_id: str = "img-1") -> list[str]:
    return [
        "--working-directory",
        str(repo),
        "--state-dir",
        str(state),
        "--timeout",
        str(timeout),
        "--image-id",
        image_id,
        "--kind",
        "codex",
    ]


def _phases(stderr: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in stderr.splitlines() if line.strip()]


def _run_subprocess(
    repo: Path,
    state: Path,
    *,
    timeout: float = 10.0,
    image_id: str = "img-1",
    env: dict[str, str] | None = None,
) -> Result:
    """Run the runner exactly as the proxy ships it: ``python -c <source>``."""
    completed = subprocess.run(
        [sys.executable, "-c", _RUNNER.read_text(encoding="utf-8")]
        + _argv(repo, state, timeout=timeout, image_id=image_id),
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
        check=False,
    )
    return completed.returncode, completed.stdout, _phases(completed.stderr)


@pytest.fixture
def run(capfd: pytest.CaptureFixture[str]) -> Callable[..., Result]:
    """Run the runner in-process (for coverage) with the same result shape."""

    def _run(
        repo: Path,
        state: Path,
        *,
        timeout: float = 10.0,
        image_id: str = "img-1",
    ) -> Result:
        capfd.readouterr()
        code = workspace_runner.main(_argv(repo, state, timeout=timeout, image_id=image_id))
        captured = capfd.readouterr()
        return code, captured.out, _phases(captured.err)

    return _run


def _write_setup(repo: Path, script: str) -> None:
    (repo / ".tth").mkdir(exist_ok=True)
    (repo / ".tth" / "setup.sh").write_text(script)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    return path


@pytest.fixture
def state(tmp_path: Path) -> Path:
    return tmp_path / "state"


def test_absent_setup_file_runs_nothing(
    repo: Path, state: Path, run: Callable[..., Result]
) -> None:
    code, out, phases = run(repo, state)

    assert code == 0
    assert out == ""
    assert phases == [{"phase": "absent", "setup_file": ".tth/setup.sh"}]
    assert not state.exists()


def test_first_run_executes_and_stamps_then_second_run_skips(
    repo: Path, state: Path, run: Callable[..., Result]
) -> None:
    _write_setup(repo, "echo hello from $PWD\n")

    code, out, phases = run(repo, state)

    assert code == 0
    assert out == f"hello from {repo}\n"
    assert [phase["phase"] for phase in phases] == ["start", "end"]
    assert phases[1]["exit_code"] == 0
    stamp = workspace_runner.read_stamp(state)
    assert stamp == phases[1]["stamp"]
    assert (state / workspace_runner.LOG_FILENAME).read_text() == out

    code, out, phases = run(repo, state)

    assert code == 0
    assert out == ""
    assert phases == [{"phase": "skip", "stamp": stamp}]


def _change_script(repo: Path) -> None:
    _write_setup(repo, "echo changed\n")


def _change_root_lockfile(repo: Path) -> None:
    (repo / "uv.lock").write_text("version = 2\n")


def _change_nested_lockfile(repo: Path) -> None:
    (repo / "frontend" / "package-lock.json").write_text("{}\n")


@pytest.mark.parametrize(
    "invalidate",
    [_change_script, _change_root_lockfile, _change_nested_lockfile],
    ids=["script", "root-lockfile", "nested-lockfile"],
)
def test_stamp_inputs_invalidate_the_stamp(
    repo: Path, state: Path, invalidate: Callable[[Path], None], run: Callable[..., Result]
) -> None:
    (repo / "frontend").mkdir()
    (repo / "frontend" / "package.json").write_text("{}\n")
    _write_setup(repo, "echo run\n")
    assert run(repo, state)[0] == 0
    assert run(repo, state)[2][0]["phase"] == "skip"

    invalidate(repo)

    assert [phase["phase"] for phase in run(repo, state)[2]] == ["start", "end"]


def test_image_change_invalidates_the_stamp(
    repo: Path, state: Path, run: Callable[..., Result]
) -> None:
    _write_setup(repo, "echo run\n")
    assert run(repo, state, image_id="img-1")[0] == 0

    assert [phase["phase"] for phase in run(repo, state, image_id="img-2")[2]] == [
        "start",
        "end",
    ]


def test_manifests_written_by_the_script_are_stamped(
    repo: Path, state: Path, run: Callable[..., Result]
) -> None:
    """npm install creates package-lock.json; the next session must still skip."""
    _write_setup(repo, "echo '{}' > package-lock.json\n")

    assert run(repo, state)[0] == 0

    assert run(repo, state)[2][0]["phase"] == "skip"


def test_stamp_ignores_hidden_and_dependency_directories(repo: Path) -> None:
    _write_setup(repo, "true\n")
    for directory in (".git", "node_modules", ".venv", "frontend"):
        (repo / directory).mkdir()
        (repo / directory / "package.json").write_text("{}\n")

    inputs = [path.relative_to(repo).as_posix() for path in workspace_runner.stamp_inputs(repo)]

    assert inputs == ["frontend/package.json"]


def test_failing_script_reports_exit_code_and_leaves_no_stamp(
    repo: Path, state: Path, run: Callable[..., Result]
) -> None:
    _write_setup(repo, "echo about to fail\nexit 7\necho unreachable\n")

    code, out, phases = run(repo, state)

    assert code == workspace_runner.EXIT_SCRIPT_FAILED
    assert out == "about to fail\n"
    assert phases[-1]["phase"] == "end"
    assert phases[-1]["exit_code"] == 7
    assert workspace_runner.read_stamp(state) is None


def test_errexit_stops_the_script_at_the_first_failure(
    repo: Path, state: Path, run: Callable[..., Result]
) -> None:
    _write_setup(repo, "false\necho unreachable\n")

    code, out, phases = run(repo, state)

    assert code == workspace_runner.EXIT_SCRIPT_FAILED
    assert out == ""
    assert phases[-1]["exit_code"] == 1


def test_timeout_kills_the_whole_process_group(
    repo: Path, state: Path, run: Callable[..., Result]
) -> None:
    marker = repo / "alive"
    _write_setup(repo, f"(sleep 30; touch {marker}) &\nsleep 30\n")

    started = time.monotonic()
    code, _, phases = run(repo, state, timeout=0.5)

    assert code == workspace_runner.EXIT_TIMEOUT
    assert phases[-1]["phase"] == "timeout"
    assert time.monotonic() - started < 15
    time.sleep(0.2)
    assert not marker.exists()
    assert workspace_runner.read_stamp(state) is None


def test_script_environment_is_a_whitelist(repo: Path, state: Path) -> None:
    _write_setup(repo, "env | sort\n")

    _, out, _ = _run_subprocess(
        repo,
        state,
        env={
            "TTH_SPLIT_TOKEN": "secret",
            "DJANGO_SETTINGS_MODULE": "tth_codex.settings",
            "OPENAI_API_KEY": "provider",
            "UV_CACHE_DIR": "/data/uv/cache",
            "npm_config_cache": "/data/npm/cache",
            "COREPACK_HOME": "/data/corepack",
            "HTTPS_PROXY": "http://tth-gateway.invalid:8080",
            "SSL_CERT_FILE": "/etc/tth/ca.pem",
        },
    )

    lines = out.splitlines()
    assert "UV_CACHE_DIR=/data/uv/cache" in lines
    assert "npm_config_cache=/data/npm/cache" in lines
    assert "COREPACK_HOME=/data/corepack" in lines
    assert "HTTPS_PROXY=http://tth-gateway.invalid:8080" in lines
    assert "SSL_CERT_FILE=/etc/tth/ca.pem" in lines
    assert "TTH_WORKSPACE_SETUP=1" in lines
    assert "TTH_HARNESS_KIND=codex" in lines
    assert "USER=agent" in lines
    assert not any(line.startswith("TTH_SPLIT_TOKEN=") for line in lines)
    assert not any(line.startswith("DJANGO_SETTINGS_MODULE=") for line in lines)
    assert not any(line.startswith("OPENAI_API_KEY=") for line in lines)


def test_setup_environment_defaults() -> None:
    env = workspace_runner.setup_environment(
        {"PATH": "/usr/bin", "HOME": "/home/agent"}, kind="grok"
    )

    assert env["PATH"] == "/usr/bin"
    assert env["HOME"] == "/home/agent"
    assert env["LANG"] == "C.UTF-8"
    assert env["TERM"] == "dumb"
    assert env["TTH_HARNESS_KIND"] == "grok"


def test_concurrent_runs_serialize_on_the_state_lock(repo: Path, state: Path) -> None:
    _write_setup(repo, "sleep 0.4\necho done\n")
    results: list[Result] = []

    def worker() -> None:
        results.append(_run_subprocess(repo, state))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    phases = sorted(str(result[2][-1]["phase"]) for result in results)
    # One run did the work; the other waited for the lock and then skipped.
    assert phases == ["end", "skip"]


def test_lock_timeout_when_another_run_holds_the_lock(
    repo: Path, state: Path, run: Callable[..., Result]
) -> None:
    _write_setup(repo, "echo run\n")
    state.mkdir()
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import fcntl, sys, time; f = open(sys.argv[1], 'a+'); "
            "fcntl.flock(f, fcntl.LOCK_EX); print('held', flush=True); time.sleep(5)",
            str(state / workspace_runner.LOCK_FILENAME),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "held"

        code, _, phases = run(repo, state, timeout=0.6)

        assert code == workspace_runner.EXIT_LOCK_TIMEOUT
        assert phases == [{"phase": "lock_timeout"}]
    finally:
        holder.kill()
        holder.wait()


def test_runner_source_runs_as_shipped_by_the_proxy(repo: Path, state: Path) -> None:
    """The proxy passes the module source to ``python -c``; ``__main__`` must fire."""
    _write_setup(repo, "echo shipped\n")

    code, out, phases = _run_subprocess(repo, state)

    assert code == 0
    assert out == "shipped\n"
    assert [phase["phase"] for phase in phases] == ["start", "end"]


def test_runner_error_is_reported_not_raised(
    repo: Path, state: Path, run: Callable[..., Result], monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_setup(repo, "true\n")

    def explode(*args: object, **kwargs: object) -> str:
        raise OSError("disk full")

    monkeypatch.setattr(workspace_runner, "compute_stamp", explode)

    code, _, phases = run(repo, state)

    assert code == workspace_runner.EXIT_RUNNER_ERROR
    assert phases == [{"phase": "error", "message": "OSError: disk full"}]


def test_read_stamp_tolerates_missing_or_malformed_records(state: Path) -> None:
    assert workspace_runner.read_stamp(state) is None
    state.mkdir()
    (state / workspace_runner.STAMP_FILENAME).write_text("not json")
    assert workspace_runner.read_stamp(state) is None
    (state / workspace_runner.STAMP_FILENAME).write_text('["list"]')
    assert workspace_runner.read_stamp(state) is None
    (state / workspace_runner.STAMP_FILENAME).write_text('{"stamp": 7}')
    assert workspace_runner.read_stamp(state) is None


def test_timeout_kills_children_that_outlive_the_shell(
    repo: Path, state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """bash dies on SIGTERM at once; a child ignoring it must still be killed."""
    monkeypatch.setattr(workspace_runner, "TERMINATE_GRACE_SECONDS", 0.2)
    state.mkdir()
    marker = repo / "escaped"
    _write_setup(repo, f"(trap '' TERM; sleep 1.5; touch {marker}) &\nwait\n")

    started = time.monotonic()
    exit_code, _ = workspace_runner.run_script(repo, state_dir=state, timeout=0.3, kind="codex")

    assert exit_code is None
    assert time.monotonic() - started < 5
    time.sleep(2)
    assert not marker.exists()

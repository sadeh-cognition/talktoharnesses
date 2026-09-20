"""Run a workspace's repo-declared ``.tth/setup.sh`` inside a sandbox container.

The proxy ships this module's source into the kind's running split container
as ``python3 -c <source> ...`` through ``docker exec`` (see
``sandbox_workspace``), so it must stay standard-library only and runnable on
the sandbox base interpreter. It is also importable on the host for tests.

Protocol with the proxy:

* stdout carries the setup script's merged stdout/stderr, verbatim;
* stderr carries one JSON object per line with a ``phase`` key:
  ``absent`` (no setup file), ``skip`` (stamp matches), ``start``,
  ``end`` (with ``exit_code`` and ``duration_ms``), ``timeout``,
  ``lock_timeout`` or ``error``;
* the exit status is 0 for absent/skip/success, 1 when the script exited
  non-zero, 2 on timeout, 3 on a runner error and 4 when the per-workspace
  lock could not be taken in time.

The stamp is a digest of the script, the container image and the workspace's
dependency manifests; it is written only after a successful run so a failed
setup is retried on the next session.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import IO, cast

SETUP_FILE = ".tth/setup.sh"
STAMP_VERSION = "1"
STAMP_FILENAME = "stamp"
LOCK_FILENAME = "lock"
LOG_FILENAME = "setup.log"
LOG_CAP_BYTES = 4 * 1024 * 1024
TERMINATE_GRACE_SECONDS = 10.0
_LOCK_POLL_SECONDS = 0.5
_GROUP_POLL_SECONDS = 0.05

EXIT_SCRIPT_FAILED = 1
EXIT_TIMEOUT = 2
EXIT_RUNNER_ERROR = 3
EXIT_LOCK_TIMEOUT = 4

# Dependency manifests and lockfiles whose content invalidates the stamp.
# Checked in the working directory and one level down (a ``frontend/`` next to
# a Python backend is the common shape).
MANIFEST_NAMES: tuple[str, ...] = (
    "pyproject.toml",
    "uv.lock",
    "poetry.lock",
    "Pipfile.lock",
    "requirements.txt",
    ".python-version",
    "package.json",
    "package-lock.json",
    "npm-shrinkwrap.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "bun.lock",
    "bun.lockb",
    ".nvmrc",
    ".node-version",
    ".tool-versions",
    "Cargo.lock",
    "go.sum",
    "Gemfile.lock",
)
_SKIPPED_DIRECTORIES = frozenset({"node_modules", ".venv"})

# Environment the script sees: the inherited entries below plus every variable
# with one of these prefixes (the toolchain caches the sandbox manager injects).
# Provider credentials, the split token and the split's Django settings never
# reach it.
_INHERITED_ENV_NAMES: tuple[str, ...] = (
    "PATH",
    "HOME",
    "LANG",
    "TERM",
    "HTTPS_PROXY",
    "https_proxy",
    "NO_PROXY",
    "no_proxy",
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
    "NODE_USE_ENV_PROXY",
)
_INHERITED_ENV_PREFIXES: tuple[str, ...] = (
    "UV_",
    "npm_config_",
    "NPM_CONFIG_",
    "COREPACK_",
    "PNPM_",
    "YARN_",
)
_ENV_DEFAULTS: dict[str, str] = {"LANG": "C.UTF-8", "TERM": "dumb"}


def stamp_inputs(working_directory: Path) -> list[Path]:
    """Manifest files that feed the stamp, in a stable order."""
    found: list[Path] = []
    for name in MANIFEST_NAMES:
        candidate = working_directory / name
        if candidate.is_file():
            found.append(candidate)
    try:
        children = sorted(child for child in working_directory.iterdir() if child.is_dir())
    except OSError:
        children = []
    for child in children:
        if child.name.startswith(".") or child.name in _SKIPPED_DIRECTORIES:
            continue
        for name in MANIFEST_NAMES:
            candidate = child / name
            if candidate.is_file():
                found.append(candidate)
    return found


def compute_stamp(working_directory: Path, *, image_id: str) -> str:
    digest = hashlib.sha256()
    digest.update(f"v{STAMP_VERSION}\0{image_id}\0".encode())
    digest.update((working_directory / SETUP_FILE).read_bytes())
    for path in stamp_inputs(working_directory):
        relative = path.relative_to(working_directory).as_posix()
        digest.update(f"\0{relative}\0".encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def setup_environment(environ: dict[str, str], *, kind: str) -> dict[str, str]:
    """The whitelist environment the setup script runs with."""
    result = dict(_ENV_DEFAULTS)
    for name in _INHERITED_ENV_NAMES:
        value = environ.get(name)
        if value:
            result[name] = value
    for name, value in environ.items():
        if name.startswith(_INHERITED_ENV_PREFIXES):
            result[name] = value
    result["USER"] = "agent"
    result["TTH_WORKSPACE_SETUP"] = "1"
    result["TTH_HARNESS_KIND"] = kind
    return result


def _emit(phase: str, **fields: object) -> None:
    sys.stderr.write(json.dumps({"phase": phase, **fields}) + "\n")
    sys.stderr.flush()


class _Tee:
    """Copies the script's output to stdout and to a capped log file."""

    def __init__(self, log_path: Path) -> None:
        self._log = log_path.open("wb")
        self._written = 0

    def write(self, chunk: bytes) -> None:
        sys.stdout.buffer.write(chunk)
        sys.stdout.buffer.flush()
        if self._written < LOG_CAP_BYTES:
            keep = chunk[: LOG_CAP_BYTES - self._written]
            self._log.write(keep)
            self._written += len(keep)

    def close(self) -> None:
        self._log.close()


def _pump(stream: IO[bytes], tee: _Tee) -> None:
    while True:
        chunk = stream.read(4096)
        if not chunk:
            return
        tee.write(chunk)


def _acquire_lock(state_dir: Path, deadline: float) -> object | None:
    # Unix-only; imported here so the proxy can import this module (to ship
    # its source) on any host, including Windows.
    import fcntl

    handle = (state_dir / LOCK_FILENAME).open("a+")
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except BlockingIOError:
            if time.monotonic() >= deadline:
                handle.close()
                return None
            time.sleep(_LOCK_POLL_SECONDS)


def run_script(
    working_directory: Path,
    *,
    state_dir: Path,
    timeout: float,
    kind: str,
) -> tuple[int | None, int]:
    """Run the setup script; returns ``(exit_code, duration_ms)``.

    ``exit_code`` is ``None`` when the script was killed for exceeding
    ``timeout``.
    """
    started = time.monotonic()
    tee = _Tee(state_dir / LOG_FILENAME)
    process = subprocess.Popen(
        ["/bin/bash", "-e", SETUP_FILE],
        cwd=working_directory,
        env=setup_environment(dict(os.environ), kind=kind),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    assert process.stdout is not None
    pump = threading.Thread(target=_pump, args=(process.stdout, tee), daemon=True)
    pump.start()
    timed_out = False
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_group(process)
    # A process that escaped the session (setsid) may keep the pipe open; do
    # not let it keep the runner alive.
    pump.join(timeout=TERMINATE_GRACE_SECONDS)
    tee.close()
    duration_ms = int((time.monotonic() - started) * 1000)
    return (None if timed_out else process.returncode, duration_ms)


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    return True


def _terminate_group(process: subprocess.Popen[bytes]) -> None:
    """SIGTERM the script's process group, then SIGKILL whatever is left.

    The shell (``start_new_session`` made it the group leader) usually dies
    on SIGTERM at once while a child that ignores the signal keeps running,
    so the grace period and the kill are tracked on the whole group, never
    on the shell alone.
    """
    pgid = process.pid
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pgid, signal.SIGTERM)
    deadline = time.monotonic() + TERMINATE_GRACE_SECONDS
    # poll() reaps the shell so a zombie leader does not keep the group "alive".
    while (process.poll() is None or _group_alive(pgid)) and time.monotonic() < deadline:
        time.sleep(_GROUP_POLL_SECONDS)
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pgid, signal.SIGKILL)
    if process.returncode is None:
        process.wait()


def _write_stamp(state_dir: Path, stamp: str, *, duration_ms: int) -> None:
    record = {"stamp": stamp, "ran_at": time.time(), "duration_ms": duration_ms}
    target = state_dir / STAMP_FILENAME
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps(record) + "\n")
    os.replace(temporary, target)


def read_stamp(state_dir: Path) -> str | None:
    try:
        record: object = json.loads((state_dir / STAMP_FILENAME).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    stamp: object = cast(dict[str, object], record).get("stamp")
    return stamp if isinstance(stamp, str) else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="run a workspace's .tth/setup.sh")
    parser.add_argument("--working-directory", required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--timeout", type=float, required=True)
    parser.add_argument("--image-id", required=True)
    parser.add_argument("--kind", required=True)
    args = parser.parse_args(argv)
    working_directory = Path(args.working_directory)
    state_dir = Path(args.state_dir)
    deadline = time.monotonic() + args.timeout

    setup_path = working_directory / SETUP_FILE
    if not setup_path.is_file():
        _emit("absent", setup_file=SETUP_FILE)
        return 0
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        lock = _acquire_lock(state_dir, deadline)
        if lock is None:
            _emit("lock_timeout")
            return EXIT_LOCK_TIMEOUT
        stamp = compute_stamp(working_directory, image_id=args.image_id)
        if read_stamp(state_dir) == stamp:
            _emit("skip", stamp=stamp)
            return 0
        _emit("start", stamp=stamp, setup_file=SETUP_FILE)
        exit_code, duration_ms = run_script(
            working_directory,
            state_dir=state_dir,
            timeout=max(0.0, deadline - time.monotonic()),
            kind=args.kind,
        )
        if exit_code == 0:
            # The script may have written manifests itself (npm install
            # creates package-lock.json); stamp what it left behind so the
            # next session skips.
            stamp = compute_stamp(working_directory, image_id=args.image_id)
            _write_stamp(state_dir, stamp, duration_ms=duration_ms)
    except Exception as exc:  # noqa: BLE001 - reported to the proxy, never raised
        _emit("error", message=f"{type(exc).__name__}: {exc}")
        return EXIT_RUNNER_ERROR
    if exit_code is None:
        _emit("timeout", duration_ms=duration_ms)
        return EXIT_TIMEOUT
    _emit("end", exit_code=exit_code, duration_ms=duration_ms, stamp=stamp)
    return 0 if exit_code == 0 else EXIT_SCRIPT_FAILED


if __name__ == "__main__":
    sys.exit(main())

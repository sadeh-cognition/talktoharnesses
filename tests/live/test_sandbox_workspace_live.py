"""Opt-in Docker gate: repo-declared workspace setup runs inside a real sandbox.

Boots a split container from a built image, mounts a throwaway repository
that pins a Python the image does not ship and carries a ``frontend/``
package, and drives ``SandboxManager.prepare_workspace`` through its stamp,
skip, failure and hygiene contracts. Needs Docker and network access for uv
and npm downloads; no provider credentials.

Enable with TALKTOHARNESSES_SANDBOX_WORKSPACE=1. The kind defaults to
``claude`` (override with TALKTOHARNESSES_SANDBOX_WORKSPACE_KIND); the image is
``tth-<kind>:$TTH_SANDBOX_IMAGE_TAG`` and must be built beforehand
(deploy/build-splits.sh <kind>).
"""

from __future__ import annotations

import os
import socket
from collections.abc import Generator
from contextlib import suppress
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest
from tth_types.enums import ErrorCode, HarnessKind
from tth_types.errors import DomainError

from talktoharnesses.remote.sandbox import SandboxConfig, SandboxManager
from talktoharnesses.remote.sandbox_workspace import (
    TOOLCHAIN_ENV,
    WorkspaceSetupStarted,
    state_directory,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("TALKTOHARNESSES_SANDBOX_WORKSPACE") != "1",
    reason="set TALKTOHARNESSES_SANDBOX_WORKSPACE=1 (requires Docker + a built split image)",
)

_KIND = HarnessKind(os.environ.get("TALKTOHARNESSES_SANDBOX_WORKSPACE_KIND", "claude"))
_SETUP_SCRIPT = """\
set -x
uv sync
.venv/bin/python --version
cd frontend && npm install --no-audit --no-fund
"""


def _available_port() -> int:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])
    finally:
        listener.close()


@pytest.fixture
def sandbox(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> Generator[tuple[SandboxManager, str], None, None]:
    """A manager bound to a throwaway container/volumes and the pytest temp root."""
    import docker
    from docker.errors import NotFound

    slug = _KIND.value.replace("_", "-")
    container_name = f"tth-live-workspace-{slug}-{uuid4().hex[:12]}"
    mount_root = str(tmp_path_factory.getbasetemp())
    monkeypatch.setenv(f"TTH_SPLIT_PORT_{_KIND.value.upper()}", str(_available_port()))

    def test_container_name(_manager: SandboxManager, _kind: HarnessKind) -> str:
        return container_name

    def test_image(manager: SandboxManager, _kind: HarnessKind) -> str:
        # The manager derives the image from the container name; keep the
        # kind's real image or a missing one is built under the test name.
        return f"tth-{slug}:{manager.config.image_tag}"

    def test_mount_roots(_manager: SandboxManager) -> tuple[str, ...]:
        return (mount_root,)

    monkeypatch.setattr(SandboxManager, "_container_name", test_container_name)
    monkeypatch.setattr(SandboxManager, "_image", test_image)
    monkeypatch.setattr(SandboxManager, "_mount_roots", test_mount_roots)
    config = SandboxConfig.from_env().model_copy(
        update={"prepare_grace": 1800.0, "workspace_setup_timeout": 600.0}
    )
    try:
        yield SandboxManager(config), container_name
    finally:
        client = docker.from_env()
        try:
            with suppress(NotFound):
                client.containers.get(container_name).remove(force=True)
            for suffix in ("home", "data"):
                with suppress(NotFound):
                    client.volumes.get(f"{container_name}-{suffix}").remove()
        finally:
            client.close()


def _exec(container_name: str, command: str) -> tuple[int, str]:
    import docker

    client = docker.from_env()
    try:
        result: Any = client.containers.get(container_name).exec_run(["sh", "-c", command])
        exit_code = cast(int | None, result.exit_code)
        output = cast(bytes, result.output)
        return (-1 if exit_code is None else exit_code), output.decode("utf-8", errors="replace")
    finally:
        client.close()


def _write_repo(root: Path) -> Path:
    repo = root / "repo"
    (repo / ".tth").mkdir(parents=True)
    (repo / "frontend").mkdir()
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0.1.0"\n'
        'requires-python = ">=3.13"\ndependencies = ["six"]\n'
    )
    (repo / "frontend" / "package.json").write_text(
        '{"name": "fe", "version": "1.0.0", "dependencies": {"is-odd": "3.0.1"}}\n'
    )
    (repo / ".tth" / "setup.sh").write_text(_SETUP_SCRIPT)
    return repo


async def test_workspace_setup_provisions_python_and_node_then_skips(
    sandbox: tuple[SandboxManager, str], tmp_path: Path
) -> None:
    manager, container_name = sandbox
    repo = _write_repo(tmp_path)
    endpoint = await manager.endpoint(_KIND, (str(repo),))
    started: list[WorkspaceSetupStarted] = []

    first = await manager.prepare_workspace(
        _KIND, str(repo), redaction_patterns=("is-odd",), on_started=started.append
    )

    assert first is not None
    assert first.status == "succeeded", first
    assert first.exit_code == 0
    assert first.duration_ms is not None and first.duration_ms > 0
    assert "Python 3.1" in first.output_tail
    assert "is-odd" not in first.output_tail, "redaction must cover setup output"
    assert [event.working_directory for event in started] == [str(repo)]
    assert first.stamp

    # The project got its own interpreter and dependencies, visible on the host
    # too (the interpreter symlink resolves only inside the container).
    assert os.path.lexists(repo / ".venv" / "bin" / "python")
    assert (repo / "frontend" / "node_modules" / "is-odd").is_dir()
    code, version = _exec(container_name, f"cd {repo} && .venv/bin/python --version")
    assert code == 0 and version.startswith("Python 3.1"), version
    assert not version.startswith("Python 3.12"), "the pinned interpreter must be downloaded"

    # Toolchain downloads and caches landed on the /data volume.
    code, listing = _exec(container_name, f"ls {TOOLCHAIN_ENV['UV_PYTHON_INSTALL_DIR']}")
    assert code == 0 and "cpython-3.1" in listing, listing
    code, _ = _exec(container_name, f"test -d {TOOLCHAIN_ENV['npm_config_cache']}")
    assert code == 0
    code, listing = _exec(container_name, f"ls {state_directory(str(repo))}")
    assert code == 0 and {"lock", "setup.log", "stamp"} <= set(listing.split()), listing

    # Hygiene: the service runtime was never touched and stays healthy.
    code, _ = _exec(container_name, "test ! -w /opt/tth/venv")
    assert code == 0, "the service venv must not be writable by the agent user"
    code, env = _exec(container_name, "env")
    assert "UV_PROJECT_ENVIRONMENT" not in env
    assert "TTH_SPLIT_TOKEN=" in env, "the container env still carries the token for the split"
    async with httpx.AsyncClient(base_url=endpoint.base_url, timeout=5.0) as client:
        health = await client.get("/v1/health")
        assert health.status_code == 200
        assert health.json()["kind"] == _KIND.value

    # A second session in the same directory finds the stamp and runs nothing.
    second = await manager.prepare_workspace(_KIND, str(repo))
    assert second is not None
    assert second.status == "skipped"
    assert second.stamp == first.stamp

    # Touching a manifest re-runs the (idempotent) script.
    (repo / "frontend" / "package.json").write_text(
        '{"name": "fe", "version": "1.0.1", "dependencies": {"is-odd": "3.0.1"}}\n'
    )
    third = await manager.prepare_workspace(_KIND, str(repo))
    assert third is not None
    assert third.status == "succeeded"
    assert third.stamp != first.stamp


async def test_failing_setup_reports_exit_status_and_tail(
    sandbox: tuple[SandboxManager, str], tmp_path: Path
) -> None:
    manager, _ = sandbox
    repo = _write_repo(tmp_path)
    (repo / ".tth" / "setup.sh").write_text("echo preparing\necho 'npm ERR! boom' >&2\nexit 7\n")
    await manager.endpoint(_KIND, (str(repo),))

    with pytest.raises(DomainError) as excinfo:
        await manager.prepare_workspace(_KIND, str(repo))

    error = excinfo.value
    assert error.code is ErrorCode.WORKSPACE_SETUP_FAILED
    assert error.details["reason"] == "exit_status"
    assert error.details["exit_code"] == 7
    assert error.details["output_tail"] == "preparing\nnpm ERR! boom\n"

    # No stamp: the next session retries the script.
    (repo / ".tth" / "setup.sh").write_text("echo fixed\n")
    outcome = await manager.prepare_workspace(_KIND, str(repo))
    assert outcome is not None
    assert outcome.status == "succeeded"
    assert outcome.output_tail == "fixed\n"


async def test_missing_setup_file_is_a_no_op(
    sandbox: tuple[SandboxManager, str], tmp_path: Path
) -> None:
    manager, _ = sandbox
    repo = tmp_path / "plain"
    repo.mkdir()
    await manager.endpoint(_KIND, (str(repo),))

    outcome = await manager.prepare_workspace(_KIND, str(repo))

    assert outcome is not None
    assert outcome.status == "absent"

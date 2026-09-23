"""Real Git and Codex sandbox checks; no model calls."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest
from codex_cli_bin import bundled_codex_path  # pyright: ignore[reportMissingTypeStubs]

from tth_codex.harness.adapter import _codex_sandbox_params  # pyright: ignore[reportPrivateUsage]
from tth_codex.harness.permissions import git_workspace_config


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    root = tmp_path / "repository"
    root.mkdir()
    git(root, "init", "-q")
    git(
        root,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@localhost",
        "commit",
        "--allow-empty",
        "-qm",
        "seed",
    )
    return root


@pytest.mark.parametrize("linked", [False, True])
def test_git_metadata_and_protected_paths(repository: Path, linked: bool) -> None:
    root = repository
    if linked:
        root = repository.parent / "linked"
        git(repository, "worktree", "add", "-qb", "feature", str(root))
    for name in (".agents", ".codex"):
        (root / name).mkdir()
        (root / name / "sentinel").write_text("unchanged")
    config = git_workspace_config(str(root))
    assert config is not None
    metadata = config["permissions"]["tth-git-workspace"]["filesystem"]
    assert set(metadata) == {
        git(root, "rev-parse", "--absolute-git-dir"),
        git(root, "rev-parse", "--path-format=absolute", "--git-common-dir"),
    }
    overrides = [
        "-c",
        'permissions.tth-git-workspace.extends=":workspace"',
        "-c",
        "permissions.tth-git-workspace.filesystem={"
        + ",".join(
            f"{json.dumps(path)} = {json.dumps(access)}" for path, access in metadata.items()
        )
        + "}",
    ]
    # Outside /tmp: that directory is deliberately writable in :workspace.
    with TemporaryDirectory(prefix="tth-codex-denied-", dir=Path.home()) as outside:
        sentinel = Path(outside) / "sentinel"
        sentinel.write_text("unchanged")
        script = """
import errno, pathlib, subprocess, socket, sys
root = pathlib.Path.cwd()
for path in [root / '.agents/sentinel', root / '.codex/sentinel', pathlib.Path(sys.argv[1])]:
    try:
        path.write_text('overwritten')
    except OSError as exc:
        assert exc.errno in (errno.EACCES, errno.EROFS, errno.EPERM)
    else:
        raise AssertionError(f'write allowed: {path}')
try:
    socket.socket().connect(('1.1.1.1', 443))
except OSError:
    pass
else:
    raise AssertionError('direct network allowed')
(root / 'solution.py').write_text('print(1)\\n')
subprocess.run(['git', 'add', 'solution.py'], check=True)
subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@localhost',
                'commit', '-qm', 'implementation'], check=True)
"""
        result = subprocess.run(
            [
                str(bundled_codex_path()),
                "sandbox",
                "-C",
                str(root),
                "-P",
                "tth-git-workspace",
                *overrides,
                "--",
                "python3",
                "-c",
                script,
                str(sentinel),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert sentinel.read_text() == "unchanged"
    assert git(root, "log", "-1", "--format=%s") == "implementation"


def test_non_repository_and_explicit_modes(tmp_path: Path, repository: Path) -> None:
    assert git_workspace_config(str(tmp_path)) is None
    for mode in ("read-only", "danger-full-access"):
        assert _codex_sandbox_params(mode, str(repository))["sandbox"].value == mode
    params = _codex_sandbox_params("workspace-write", str(repository))
    assert "sandbox" not in params
    assert params["config"]["default_permissions"] == "tth-git-workspace"


@pytest.mark.asyncio
async def test_start_and_resume_select_git_profile(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from tth_codex.harness.adapter import (
        _build_broker_async_codex,  # pyright: ignore[reportPrivateUsage]
    )

    client = _build_broker_async_codex(None, yolo=True)
    requests: list[Any] = []

    async def initialized() -> None:
        pass

    async def start(params: Any) -> Any:
        requests.append(params)
        return SimpleNamespace(thread=SimpleNamespace(id="test"))

    async def resume(thread_id: str, params: Any) -> None:
        assert thread_id == "test"
        requests.append(params)

    monkeypatch.setattr(client, "_ensure_initialized", initialized)
    monkeypatch.setattr(client._client, "thread_start", start)
    monkeypatch.setattr(client._client, "thread_resume", resume)
    await client.thread_start(cwd=str(repository), sandbox="workspace-write")
    await client.thread_resume("test", cwd=str(repository), sandbox="workspace-write")
    assert len(requests) == 2
    for request in requests:
        assert request.sandbox is None
        assert request.approval_policy.root.value == "never"
        assert (
            request.model_dump(mode="json")["config"]["default_permissions"] == "tth-git-workspace"
        )

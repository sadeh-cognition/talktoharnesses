"""Fixtures for the supervised-runtime tests (paths, supervisor, redaction)."""

from __future__ import annotations

from pathlib import Path

import pytest
from tth_types.enums import HarnessKind
from tth_types.harness import HarnessCapabilities, LaunchSnapshot

from tests.runtime.helpers import child_modes_path as _child_modes_path
from tests.runtime.helpers import copy_owned_executable


@pytest.fixture
def owned_python(tmp_path: Path) -> Path:
    return copy_owned_executable(tmp_path / "bin")


@pytest.fixture(autouse=True)
def workdir(tmp_path: Path) -> Path:
    d = tmp_path / "ws"
    d.mkdir()
    return d


def child_modes_path() -> Path:
    return _child_modes_path()


def make_launch(
    *,
    executable: Path,
    workdir: Path,
    version: str = "test-1",
) -> LaunchSnapshot:
    caps = HarnessCapabilities(kind=HarnessKind.OPENCODE, version=version)
    return LaunchSnapshot(
        resolved_executable=str(executable.resolve()),
        harness_version=version,
        working_directory=str(workdir.resolve()),
        workspace_roots=(str(workdir.resolve()),),
        model="m",
        mode="default",
        adapter_version="0",
        capabilities=caps,
    )

"""Path normalization edge cases for approval matching."""

from __future__ import annotations

from pathlib import Path

import pytest

from talktoharnesses.domain import (
    FileApprovalAction,
    FileOperation,
    normalize_approval_action,
    normalize_approval_path,
    normalize_directory,
    path_is_under_directory,
)


def test_relative_path_requires_working_directory() -> None:
    with pytest.raises(ValueError, match="working_directory"):
        normalize_approval_path("rel.txt")


def test_create_target_allows_missing_final(tmp_path: Path) -> None:
    parent = tmp_path / "dir"
    parent.mkdir()
    target = parent / "newfile.txt"
    resolved = normalize_approval_path(
        str(target),
        allow_missing_final=True,
    )
    assert resolved.endswith("newfile.txt")
    assert Path(resolved).parent == parent.resolve()


def test_create_action_normalizes_missing_leaf(tmp_path: Path) -> None:
    parent = tmp_path / "p"
    parent.mkdir()
    action = FileApprovalAction(
        path=str(parent / "leaf"),
        operation=FileOperation.CREATE,
    )
    out = normalize_approval_action(action, working_directory=None)
    assert out is not None
    assert out.path.endswith("leaf")  # type: ignore[union-attr]


def test_symlink_components_resolved(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(real)
    except OSError:
        pytest.skip("symlinks not supported")
    f = real / "a.txt"
    f.write_text("x")
    via_link = normalize_approval_path(str(link / "a.txt"))
    assert Path(via_link) == f.resolve()


def test_sibling_prefix_not_contained(tmp_path: Path) -> None:
    a = tmp_path / "proj"
    b = tmp_path / "proj-other"
    a.mkdir()
    b.mkdir()
    assert not path_is_under_directory(str(b.resolve()), str(a.resolve()))


def test_normalize_directory_rejects_file(tmp_path: Path) -> None:
    f = tmp_path / "f.txt"
    f.write_text("x")
    with pytest.raises(ValueError, match="not a directory"):
        normalize_directory(str(f))

"""Workspace permissions for Git-backed Codex sessions."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any


def git_workspace_config(cwd: str) -> dict[str, Any] | None:
    """Allow this repository's metadata without removing the workspace sandbox.

    The proxy has already admitted the working directory and its repository
    into the Project's Docker mount set. Git resolves both ordinary repositories
    and linked worktrees; no parent directory becomes writable here.
    """
    result = subprocess.run(
        [
            "git",
            "-C",
            cwd,
            "rev-parse",
            "--path-format=absolute",
            "--git-dir",
            "--git-common-dir",
        ],
        env={key: value for key, value in os.environ.items() if not key.startswith("GIT_")},
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if result.returncode:
        return None
    metadata = {str(Path(path).resolve()): "write" for path in result.stdout.splitlines()}
    return {
        "default_permissions": "tth-git-workspace",
        "permissions": {
            "tth-git-workspace": {
                "extends": ":workspace",
                "filesystem": metadata,
            },
        },
    }

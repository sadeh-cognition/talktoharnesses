"""Resolve repository identity on the host before admitting sandbox mounts."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def repository_directory(root: Path) -> str | None:
    result = subprocess.run(
        [
            "git",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=/dev/null",
            "-C",
            str(root),
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
        ],
        env={
            **{key: value for key, value in os.environ.items() if not key.startswith("GIT_")},
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    return str(Path(result.stdout.strip()).resolve()) if result.returncode == 0 else None

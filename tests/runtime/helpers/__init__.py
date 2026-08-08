"""Runtime test helpers."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path


def copy_owned_executable(dest_dir: Path, name: str = "test-python") -> Path:
    """Create a user-owned executable that execs the real test interpreter.

    A byte-for-byte copy of ``sys.executable`` often cannot bootstrap (prefix
    paths break). A small owned shell wrapper keeps ownership/execute checks
    real while still running the live interpreter.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / name
    # Quote the interpreter path for spaces; pass remaining argv through.
    dest.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "$@"\n',
        encoding="utf-8",
    )
    dest.chmod(stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
    if hasattr(os, "geteuid") and dest.stat().st_uid != os.geteuid():
        msg = f"wrapper executable not owned by euid: {dest}"
        raise RuntimeError(msg)
    return dest.resolve()


def child_modes_path() -> Path:
    return Path(__file__).resolve().parent / "child_modes.py"

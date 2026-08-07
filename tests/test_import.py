"""Core package import smoke tests (no Django required)."""

from __future__ import annotations

import os
import subprocess
import sys
from importlib import metadata, resources


def test_version() -> None:
    import talktoharnesses

    assert talktoharnesses.__version__ == metadata.version("talktoharnesses")


def test_py_typed_present() -> None:
    root = resources.files("talktoharnesses")
    assert (root / "py.typed").is_file()


def test_core_import_does_not_load_django() -> None:
    """Importing talktoharnesses must not pull Django as a side effect."""
    code = """
import sys
from importlib.metadata import version

import talktoharnesses

assert talktoharnesses.__version__ == version("talktoharnesses")
assert "django" not in sys.modules, sorted(sys.modules)
"""
    # Fresh interpreter so other tests that imported Django cannot leak.
    result = subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )
    assert result.returncode == 0, result.stdout + result.stderr

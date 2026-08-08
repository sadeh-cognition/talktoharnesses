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


def test_domain_layer_import_does_not_load_django() -> None:
    code = """
import sys

import talktoharnesses.domain
import talktoharnesses.application
import talktoharnesses.providers

assert talktoharnesses.domain.ErrorCode.PERSISTENCE_REQUIRED.value == "persistence_required"
assert "django" not in sys.modules, sorted(sys.modules)
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_runtime_package_import_does_not_load_django() -> None:
    code = """
import sys

import talktoharnesses.runtime

assert talktoharnesses.runtime.ProcessSupervisor is not None
assert talktoharnesses.runtime.RuntimeManager is not None
assert "django" not in sys.modules, sorted(sys.modules)
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_acp_and_grok_import_do_not_load_django() -> None:
    code = """
import sys

import talktoharnesses.providers.acp
import talktoharnesses.providers.grok

assert talktoharnesses.providers.acp.AcpConnection is not None
assert talktoharnesses.providers.grok.GrokAdapter is not None
assert "django" not in sys.modules, sorted(sys.modules)
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_supported_harnesses_markdown_drift() -> None:
    from pathlib import Path

    from talktoharnesses.providers.grok.compatibility import (
        load_grok_compatibility,
        render_supported_harnesses_markdown,
    )

    root = Path(__file__).resolve().parents[1]
    path = root / "SUPPORTED_HARNESSES.md"
    assert path.is_file()
    expected = render_supported_harnesses_markdown(load_grok_compatibility())
    assert path.read_text(encoding="utf-8") == expected

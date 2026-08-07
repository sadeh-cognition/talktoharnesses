"""Wheel/sdist content smoke tests."""

from __future__ import annotations

import subprocess
import tarfile
import zipfile
from importlib.metadata import version
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def dist_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("dist")
    result = subprocess.run(
        ["uv", "build", "--no-sources", "--out-dir", str(out)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return out


def _wheel(dist_dir: Path) -> Path:
    wheels = list(dist_dir.glob("*.whl"))
    assert len(wheels) == 1, wheels
    return wheels[0]


def _sdist(dist_dir: Path) -> Path:
    sdists = list(dist_dir.glob("*.tar.gz"))
    assert len(sdists) == 1, sdists
    return sdists[0]


def _run_with_wheel(wheel: Path, requirement: str, code: str) -> None:
    result = subprocess.run(
        [
            "uv",
            "run",
            "--isolated",
            "--no-project",
            "--with",
            requirement,
            "--",
            "python",
            "-c",
            code,
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_wheel_contains_package_metadata_and_py_typed(dist_dir: Path) -> None:
    wheel = _wheel(dist_dir)
    with zipfile.ZipFile(wheel) as zf:
        names = set(zf.namelist())
        assert any(n.endswith("py.typed") and "talktoharnesses" in n for n in names)
        metadata_names = [n for n in names if n.endswith(".dist-info/METADATA")]
        assert len(metadata_names) == 1
        metadata = zf.read(metadata_names[0]).decode()
        assert "Name: talktoharnesses" in metadata
        assert f"Version: {version('talktoharnesses')}" in metadata
        assert "License: MIT" in metadata or "License-Expression: MIT" in metadata
        assert "unified coding-agent harness" in metadata.lower() or "Summary:" in metadata


def test_sdist_contains_license_and_readme(dist_dir: Path) -> None:
    sdist = _sdist(dist_dir)
    with tarfile.open(sdist, "r:gz") as tf:
        names = set(tf.getnames())
        assert any(n.endswith("LICENSE") or n.endswith("LICENSE.md") for n in names)
        assert any(n.endswith("README.md") for n in names)
        assert any("pyproject.toml" in n for n in names)
        assert any("py.typed" in n for n in names)


def test_core_wheel_installs_without_django(dist_dir: Path) -> None:
    wheel = _wheel(dist_dir)
    _run_with_wheel(
        wheel,
        str(wheel),
        """
from importlib.util import find_spec
import talktoharnesses

assert talktoharnesses.__version__
assert find_spec("django") is None
""",
    )


def test_django_extra_installs_and_initializes(dist_dir: Path) -> None:
    wheel = _wheel(dist_dir)
    _run_with_wheel(
        wheel,
        f"talktoharnesses[django] @ {wheel.as_uri()}",
        """
import django
from django.apps import apps
from django.conf import settings
from django.core.checks import run_checks

settings.configure(
    SECRET_KEY="test-not-for-production",
    INSTALLED_APPS=["django.contrib.contenttypes", "talktoharnesses.django"],
    DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}},
)
django.setup()
assert apps.get_app_config("talktoharnesses").name == "talktoharnesses.django"
assert run_checks() == []
""",
    )

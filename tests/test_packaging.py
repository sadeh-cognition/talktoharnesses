"""Wheel/sdist content and isolated install matrix."""

from __future__ import annotations

import subprocess
import tarfile
import zipfile
from importlib.metadata import version
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMPAT_FILES = (
    "grok.json",
    "cursor.json",
    "codex.json",
    "claude.json",
    "opencode.json",
    "prime_agent.json",
)
MIGRATIONS = ("0001_initial.py",)
REQUIRED_EXTRAS = (
    "django",
    "postgres",
    "client",
    "grok",
    "cursor",
    "codex",
    "claude",
    "opencode",
    "prime-agent",
    "all",
)


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


def _run_isolated(requirement: str, code: str, *, cwd: Path | None = None) -> None:
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
        cwd=cwd if cwd is not None else Path("/tmp"),
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
        for extra in REQUIRED_EXTRAS:
            assert f"Provides-Extra: {extra}" in metadata
        assert "Provides-Extra: otel" not in metadata
        for name in COMPAT_FILES:
            assert any(n.endswith(f"talktoharnesses/data/compatibility/{name}") for n in names), (
                name
            )
        for name in MIGRATIONS:
            assert any(n.endswith(f"talktoharnesses/django/migrations/{name}") for n in names), name
        forbidden_fragments = (
            "/tests/",
            ".coverage",
            "opentelemetry/sdk",
            "credentials.json",
            "/home/",
        )
        for fragment in forbidden_fragments:
            assert not any(fragment in n for n in names), fragment


def test_sdist_contains_license_readme_and_package_data(dist_dir: Path) -> None:
    sdist = _sdist(dist_dir)
    with tarfile.open(sdist, "r:gz") as tf:
        names = set(tf.getnames())
        assert any(n.endswith("LICENSE") or n.endswith("LICENSE.md") for n in names)
        assert any(n.endswith("README.md") for n in names)
        assert any(n.endswith("pyproject.toml") for n in names)
        assert any("py.typed" in n for n in names)
        for name in COMPAT_FILES:
            assert any(n.endswith(f"data/compatibility/{name}") for n in names), name
        for name in MIGRATIONS:
            assert any(n.endswith(f"django/migrations/{name}") for n in names), name


def test_core_wheel_installs_without_django(dist_dir: Path) -> None:
    wheel = _wheel(dist_dir)
    _run_isolated(
        str(wheel),
        """
from importlib.util import find_spec
import talktoharnesses
import talktoharnesses.domain
import talktoharnesses.application
import talktoharnesses.providers
import talktoharnesses.runtime

assert talktoharnesses.__version__
assert find_spec("django") is None
assert find_spec("jwt") is None
assert find_spec("ninja") is None
assert find_spec("psycopg") is None
assert find_spec("httpx") is None
assert find_spec("openai_codex") is None
assert find_spec("claude_agent_sdk") is None

try:
    import talktoharnesses.client  # noqa: F401
except ModuleNotFoundError as exc:
    assert "talktoharnesses[client]" in str(exc)
else:
    raise AssertionError("expected ModuleNotFoundError for talktoharnesses.client")
""",
    )


def test_client_extra_installs_without_django(dist_dir: Path) -> None:
    wheel = _wheel(dist_dir)
    _run_isolated(
        f"talktoharnesses[client] @ {wheel.as_uri()}",
        """
import asyncio
from importlib.util import find_spec
from talktoharnesses.client import APIError, AsyncTalkToHarnessesClient, ConversationStreamItem

assert find_spec("httpx") is not None
assert find_spec("django") is None
assert find_spec("ninja") is None
assert find_spec("jwt") is None
assert find_spec("psycopg") is None
assert find_spec("openai_codex") is None
assert find_spec("claude_agent_sdk") is None
assert APIError is not None
assert ConversationStreamItem is not None

async def _run() -> None:
    client = AsyncTalkToHarnessesClient("http://example.invalid/api/v1/")
    await client.aclose()

asyncio.run(_run())
""",
    )


def test_django_extra_installs_and_initializes(dist_dir: Path) -> None:
    wheel = _wheel(dist_dir)
    _run_isolated(
        f"talktoharnesses[django] @ {wheel.as_uri()}",
        """
from importlib.util import find_spec
import django
from django.apps import apps
from django.conf import settings
from django.core.checks import run_checks

settings.configure(
    SECRET_KEY="test-not-for-production",
    INSTALLED_APPS=[
        "django.contrib.contenttypes",
        "django.contrib.auth",
        "talktoharnesses.django",
    ],
    DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}},
)
django.setup()
assert apps.get_app_config("talktoharnesses").name == "talktoharnesses.django"
assert run_checks() == []
assert find_spec("psycopg") is None
assert find_spec("openai_codex") is None
assert find_spec("claude_agent_sdk") is None
""",
    )


def test_all_extra_installs_providers_and_postgres_driver(dist_dir: Path) -> None:
    wheel = _wheel(dist_dir)
    _run_isolated(
        f"talktoharnesses[all] @ {wheel.as_uri()}",
        """
from importlib.util import find_spec
import django
from django.conf import settings
from talktoharnesses.providers.claude import ClaudeAdapter
from talktoharnesses.providers.codex import CodexAdapter
from talktoharnesses.providers.cursor import CursorAdapter
from talktoharnesses.providers.grok import GrokAdapter
from talktoharnesses.providers.opencode import OpenCodeAdapter
from talktoharnesses.providers.prime_agent import PrimeAgentAdapter
from talktoharnesses.providers.claude.compatibility import load_claude_compatibility
from talktoharnesses.providers.codex.compatibility import load_codex_compatibility
from talktoharnesses.providers.cursor.compatibility import load_cursor_compatibility
from talktoharnesses.providers.grok.compatibility import load_grok_compatibility
from talktoharnesses.providers.opencode.compatibility import load_opencode_compatibility
from talktoharnesses.providers.prime_agent.compatibility import load_prime_agent_compatibility

settings.configure(
    SECRET_KEY="test-not-for-production",
    INSTALLED_APPS=[
        "django.contrib.contenttypes",
        "django.contrib.auth",
        "talktoharnesses.django",
    ],
    DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}},
)
django.setup()
assert all(
    (ClaudeAdapter, CodexAdapter, CursorAdapter, GrokAdapter, OpenCodeAdapter, PrimeAgentAdapter)
)
assert load_grok_compatibility().releases
assert load_cursor_compatibility().releases
assert load_codex_compatibility().releases
assert load_claude_compatibility().releases
assert load_opencode_compatibility().releases
assert load_prime_agent_compatibility().releases
assert find_spec("psycopg") is not None
""",
    )


def test_sdist_installs_and_imports(dist_dir: Path) -> None:
    sdist = _sdist(dist_dir)
    _run_isolated(
        str(sdist),
        """
import talktoharnesses
import talktoharnesses.domain
import talktoharnesses.application
import talktoharnesses.providers
import talktoharnesses.runtime

assert talktoharnesses.__version__
""",
    )

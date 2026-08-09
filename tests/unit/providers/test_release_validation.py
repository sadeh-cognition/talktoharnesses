"""Release-oriented compatibility validation checks."""

from __future__ import annotations

from talktoharnesses.providers.compatibility import (
    is_development_version,
    validate_compatibility_documents,
)
from talktoharnesses.providers.render_supported import main


def test_development_version_detection() -> None:
    assert is_development_version("2026.8.1.dev1")
    assert not is_development_version("2026.8.0")


def test_development_validate_cli_flag() -> None:
    assert main(["--validate", "development", "--check"]) == 0


def test_stable_validate_cli_flag_passes_for_published_release() -> None:
    validate_compatibility_documents(mode="stable")
    assert main(["--validate", "stable", "--check"]) == 0

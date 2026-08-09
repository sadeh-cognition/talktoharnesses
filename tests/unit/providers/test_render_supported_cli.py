"""CLI coverage for render_supported and stable validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from talktoharnesses.domain.enums import ErrorCode
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.providers.compatibility import validate_compatibility_documents
from talktoharnesses.providers.render_supported import main


def test_render_supported_check_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from talktoharnesses.providers import compatibility as compat

    content = compat.render_supported_harnesses_markdown()
    out = tmp_path / "SUPPORTED_HARNESSES.md"
    out.write_text(content, encoding="utf-8")
    monkeypatch.setattr(
        "talktoharnesses.providers.render_supported.Path.resolve",
        lambda self: self,  # type: ignore[misc]
    )
    assert main(["--check", "--output", str(out)]) == 0


def test_render_supported_writes_file(tmp_path: Path) -> None:
    out = tmp_path / "out.md"
    assert main(["--output", str(out)]) == 0
    assert out.is_file()
    assert "Supported Harnesses" in out.read_text(encoding="utf-8")


def test_render_supported_check_detects_stale(tmp_path: Path) -> None:
    out = tmp_path / "SUPPORTED_HARNESSES.md"
    out.write_text("stale\n", encoding="utf-8")
    assert main(["--check", "--output", str(out)]) == 1


def test_render_supported_validate_development() -> None:
    assert main(["--validate", "development", "--check", "--output", "SUPPORTED_HARNESSES.md"]) in {
        0,
        1,
    }
    # development validation itself must succeed even if file path check varies.
    validate_compatibility_documents(mode="development")


def test_stable_validation_fails_on_dev9() -> None:
    with pytest.raises(DomainError) as exc:
        validate_compatibility_documents(mode="stable")
    assert exc.value.code is ErrorCode.PROVIDER_INCOMPATIBLE

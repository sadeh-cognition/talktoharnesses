"""Codex compatibility source tests."""

from __future__ import annotations

import pytest

from talktoharnesses.domain.enums import ErrorCode
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.providers.codex.compatibility import (
    load_codex_compatibility,
    match_release,
)
from talktoharnesses.providers.compatibility import render_supported_harnesses_markdown


def test_load_and_match_release() -> None:
    doc = load_codex_compatibility()
    assert doc.adapter_version == "2026.8.1"
    assert doc.create_matrix
    assert doc.resume_matrix
    release = match_release(sdk_version="0.144.4", runtime_version="0.144.4", platform="linux")
    assert release.id == "codex-openai-codex-0.144.4"
    assert release.capabilities.supports_steer is True


def test_unknown_version_fails() -> None:
    with pytest.raises(DomainError) as exc:
        match_release(sdk_version="9.9.9", runtime_version="9.9.9")
    assert exc.value.code is ErrorCode.PROVIDER_INCOMPATIBLE


def test_markdown_includes_codex() -> None:
    md = render_supported_harnesses_markdown()
    assert "## Codex" in md
    assert "codex-openai-codex-0.144.4" in md
    assert "approval" in md.lower()

"""Grok compatibility source tests."""

from __future__ import annotations

import pytest

from talktoharnesses.domain.enums import ErrorCode
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.providers.grok.argv import build_grok_argv
from talktoharnesses.providers.grok.compatibility import (
    load_grok_compatibility,
    match_release,
    parse_version_stdout,
    render_supported_harnesses_markdown,
)


def test_load_and_match_release() -> None:
    doc = load_grok_compatibility()
    assert doc.adapter_version == "2026.8.0.dev7"
    release = match_release("grok 1.0.0 (3cd0d0cbce) [stable]", platform="linux")
    assert release.id == "grok-1.0.0-3cd0d0cbce"
    caps = release.to_harness_capabilities()
    assert caps.supports_resume is True
    assert caps.supports_steer is False


def test_unknown_version_fails() -> None:
    with pytest.raises(DomainError) as exc:
        match_release("grok 9.9.9 (deadbeef) [stable]")
    assert exc.value.code is ErrorCode.PROVIDER_INCOMPATIBLE


def test_malformed_version_fails() -> None:
    with pytest.raises(DomainError) as exc:
        parse_version_stdout("not a version")
    assert exc.value.code is ErrorCode.PROVIDER_INCOMPATIBLE


@pytest.mark.parametrize(
    "output",
    [
        "grok 1.0.0 (3cd0d0cbce) unexpected",
        "grok 1.0.0 (3cd0d0cbce)\nextra identity",
    ],
)
def test_version_output_must_match_completely(output: str) -> None:
    with pytest.raises(DomainError) as exc:
        match_release(output)
    assert exc.value.code is ErrorCode.PROVIDER_INCOMPATIBLE


def test_markdown_render_empty_matrices() -> None:
    md = render_supported_harnesses_markdown()
    assert "Supported Harnesses" in md
    assert "grok-1.0.0-3cd0d0cbce" in md
    assert "No published create combinations" in md


def test_build_argv() -> None:
    assert build_grok_argv() == ("agent", "--no-leader", "stdio")
    assert build_grok_argv(model="grok-build") == (
        "agent",
        "--no-leader",
        "--model",
        "grok-build",
        "stdio",
    )
    assert "--always-approve" not in build_grok_argv(model="x")

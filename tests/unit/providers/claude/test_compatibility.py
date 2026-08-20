"""Claude compatibility source tests."""

from __future__ import annotations

import pytest

from talktoharnesses.domain.enums import ErrorCode
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.providers.claude.compatibility import (
    load_claude_compatibility,
    match_release,
)
from talktoharnesses.providers.compatibility import render_supported_harnesses_markdown


def test_load_and_match_release() -> None:
    doc = load_claude_compatibility()
    assert doc.adapter_version == "2026.8.5"
    release = match_release(
        sdk_version="0.1.53",
        cli_version="2.1.88",
        cli_source="bundled",
        platform="linux",
    )
    assert release.id == "claude-agent-sdk-0.1.53-bundled-2.1.88"
    assert release.capabilities.supports_steer is False


def test_newer_cli_above_floor_is_accepted() -> None:
    release = match_release(
        sdk_version="0.1.53",
        cli_version="2.1.90",
        cli_source="explicit",
        platform="linux",
    )
    assert release.cli_version == "2.1.90"


def test_unknown_sdk_fails() -> None:
    with pytest.raises(DomainError) as exc:
        match_release(sdk_version="0.0.1", cli_version="2.1.88")
    assert exc.value.code is ErrorCode.PROVIDER_INCOMPATIBLE


def test_cli_below_floor_fails() -> None:
    with pytest.raises(DomainError) as exc:
        match_release(sdk_version="0.1.53", cli_version="2.1.0")
    assert exc.value.code is ErrorCode.PROVIDER_INCOMPATIBLE


def test_markdown_includes_claude() -> None:
    md = render_supported_harnesses_markdown()
    assert "## Claude Code" in md
    assert "claude-agent-sdk-0.1.53-bundled-2.1.88" in md

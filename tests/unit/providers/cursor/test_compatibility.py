"""Cursor compatibility source tests."""

from __future__ import annotations

import pytest

from talktoharnesses.domain.enums import ErrorCode
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.providers.compatibility import render_supported_harnesses_markdown
from talktoharnesses.providers.cursor.argv import build_cursor_argv
from talktoharnesses.providers.cursor.compatibility import (
    load_cursor_compatibility,
    match_release,
    parse_version_stdout,
)


def test_load_and_match_release() -> None:
    doc = load_cursor_compatibility()
    assert doc.adapter_version == "2026.8.3"
    release = match_release("2026.08.04-aaa8809", platform="linux")
    assert release.id == "cursor-2026.08.04-aaa8809"
    caps = release.to_harness_capabilities()
    assert caps.supports_resume is True
    assert caps.supports_steer is False
    assert "session/set_config_option" in release.required_agent_methods
    assert "clientCapabilities._meta.parameterizedModelPicker" in release.allowlisted_extensions


def test_unknown_version_fails() -> None:
    with pytest.raises(DomainError) as exc:
        match_release("2099.01.01-deadbeef")
    assert exc.value.code is ErrorCode.PROVIDER_INCOMPATIBLE


def test_malformed_version_fails() -> None:
    with pytest.raises(DomainError) as exc:
        parse_version_stdout("line1\nline2")
    assert exc.value.code is ErrorCode.PROVIDER_INCOMPATIBLE


def test_markdown_includes_cursor() -> None:
    md = render_supported_harnesses_markdown()
    assert "## Cursor" in md
    assert "cursor-2026.08.04-aaa8809" in md


def test_build_argv_accepts_no_model_mode_flags() -> None:
    assert build_cursor_argv() == ("acp",)
    assert build_cursor_argv(yolo=True) == ("acp", "--yolo")

"""Grok compatibility floor tests."""

from __future__ import annotations

import pytest

from talktoharnesses.domain.enums import ErrorCode, HarnessKind
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.domain.models import HarnessConfiguration
from talktoharnesses.providers.grok.adapter import GrokAdapter
from talktoharnesses.providers.grok.argv import build_grok_argv
from talktoharnesses.providers.grok.compatibility import (
    load_grok_compatibility,
    match_release,
    parse_version_stdout,
    render_supported_harnesses_markdown,
)


def test_load_and_match_release() -> None:
    doc = load_grok_compatibility()
    assert doc.adapter_version == "2026.8.5"
    assert doc.floor.version == "1.0.0"
    release = match_release("grok 1.0.0 (3cd0d0cbce) [stable]", platform="linux")
    assert release.id == "grok-1.0.0-3cd0d0cbce"
    caps = release.to_harness_capabilities()
    assert caps.supports_resume is True
    assert caps.supports_steer is False
    assert [effort.id for effort in caps.efforts] == ["low", "medium", "high"]


def test_match_newer_patch_above_floor() -> None:
    release = match_release("grok 1.0.6 (deadbeef) [stable]", platform="linux")
    assert release.id == "grok-1.0.6-deadbeef"
    assert release.cli_version == "1.0.6"


def test_match_1_0_5_release() -> None:
    release = match_release("grok 1.0.5 (5115b46bc9) [stable]", platform="linux")
    assert release.id == "grok-1.0.5-5115b46bc9"


def test_below_floor_fails() -> None:
    with pytest.raises(DomainError) as exc:
        match_release("grok 0.9.0 (deadbeef) [stable]")
    assert exc.value.code is ErrorCode.PROVIDER_INCOMPATIBLE
    assert "floor" in str(exc.value).lower()


def test_unpublished_platform_fails() -> None:
    with pytest.raises(DomainError) as exc:
        match_release("grok 1.0.5 (5115b46bc9) [stable]", platform="darwin")
    assert exc.value.code is ErrorCode.PROVIDER_INCOMPATIBLE
    assert exc.value.details["supported_platforms"] == ["linux"]


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


def test_markdown_render_floor() -> None:
    md = render_supported_harnesses_markdown()
    assert "Supported Harnesses" in md
    assert "1.0.0" in md
    assert "linux" in md
    assert "No published create combinations" not in md


def test_build_argv() -> None:
    assert build_grok_argv() == (
        "--permission-mode",
        "default",
        "agent",
        "--no-leader",
        "stdio",
    )
    assert build_grok_argv(model="grok-build") == (
        "--permission-mode",
        "default",
        "agent",
        "--no-leader",
        "--model",
        "grok-build",
        "stdio",
    )
    assert "--reasoning-effort" in build_grok_argv(effort="high")
    assert "--always-approve" not in build_grok_argv(model="x")
    assert build_grok_argv(yolo=True) == (
        "--always-approve",
        "agent",
        "--no-leader",
        "stdio",
    )
    assert build_grok_argv(model="grok-build", yolo=True) == (
        "--always-approve",
        "agent",
        "--no-leader",
        "--model",
        "grok-build",
        "stdio",
    )
    assert "--permission-mode" not in build_grok_argv(yolo=True)


def test_adapter_build_argv_maps_yolo_on_create_and_resume() -> None:
    adapter = GrokAdapter()
    default = HarnessConfiguration(
        kind=HarnessKind.GROK,
        working_directory="/tmp",
        model="grok-build",
        effort="high",
    )
    yolo = HarnessConfiguration(
        kind=HarnessKind.GROK,
        working_directory="/tmp",
        model="grok-build",
        yolo=True,
    )
    assert adapter.build_argv(default) == build_grok_argv(model="grok-build", effort="high")
    assert adapter.build_argv(yolo) == build_grok_argv(model="grok-build", yolo=True)

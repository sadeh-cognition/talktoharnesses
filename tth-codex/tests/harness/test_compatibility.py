"""Codex compatibility source tests."""

from __future__ import annotations

import pytest
from tth_types.enums import ErrorCode
from tth_types.errors import DomainError

from tth_codex.harness.compatibility import (
    load_codex_compatibility,
    match_release,
)


def test_load_and_match_release() -> None:
    doc = load_codex_compatibility()
    assert doc.adapter_version == "2026.8.5"
    assert doc.floor.version == "0.144.4"
    release = match_release(sdk_version="0.144.4", runtime_version="0.144.4", platform="linux")
    assert release.id == "codex-openai-codex-0.144.4"
    assert release.capabilities.supports_steer is True


def test_unknown_version_fails() -> None:
    with pytest.raises(DomainError) as exc:
        match_release(sdk_version="9.9.9", runtime_version="9.9.9")
    assert exc.value.code is ErrorCode.PROVIDER_INCOMPATIBLE

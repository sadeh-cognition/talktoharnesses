"""Shared exact create/resume matrix validation and membership tests."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from talktoharnesses.domain.enums import ErrorCode
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.providers.claude.compatibility import load_claude_compatibility
from talktoharnesses.providers.codex.compatibility import load_codex_compatibility
from talktoharnesses.providers.compatibility import (
    CompatibilityMatrixEntry,
    ReleaseCapabilities,
    assert_matrix_membership,
    is_development_version,
    render_supported_harnesses_markdown,
    validate_compatibility_documents,
    validate_matrices,
)
from talktoharnesses.providers.cursor.compatibility import load_cursor_compatibility
from talktoharnesses.providers.grok.compatibility import load_grok_compatibility
from talktoharnesses.providers.opencode.compatibility import load_opencode_compatibility
from talktoharnesses.providers.prime_agent.compatibility import load_prime_agent_compatibility


class _Release:
    def __init__(
        self,
        release_id: str,
        *,
        platforms: list[str],
        supports_resume: bool = True,
        supports_steer: bool = False,
        supports_interrupt: bool = True,
        supports_multi_interaction: bool = False,
        supports_nested_activity: bool = False,
    ) -> None:
        self.id = release_id
        self.platforms = platforms
        self.capabilities = ReleaseCapabilities(
            supports_resume=supports_resume,
            supports_steer=supports_steer,
            supports_interrupt=supports_interrupt,
            supports_multi_interaction=supports_multi_interaction,
            supports_nested_activity=supports_nested_activity,
        )


PROVIDERS = (
    ("grok", load_grok_compatibility),
    ("cursor", load_cursor_compatibility),
    ("codex", load_codex_compatibility),
    ("claude", load_claude_compatibility),
    ("opencode", load_opencode_compatibility),
    ("prime_agent", load_prime_agent_compatibility),
)


@pytest.mark.parametrize("label,loader", PROVIDERS)
def test_packaged_documents_load_with_published_matrices(label: str, loader: Any) -> None:
    del label
    doc = loader()
    assert doc.adapter_version == "2026.8.4"
    assert doc.create_matrix
    assert doc.resume_matrix
    assert doc.interrupt_matrix
    assert doc.releases
    advertised = doc.releases[0].capabilities
    if advertised.supports_steer:
        assert doc.steer_matrix
    else:
        assert not doc.steer_matrix
    if advertised.supports_multi_interaction:
        assert doc.multi_interaction_matrix
    else:
        assert not doc.multi_interaction_matrix
    assert advertised.supports_nested_activity is False
    assert not doc.nested_activity_matrix


def test_development_validation_allows_packaged_matrices() -> None:
    validate_compatibility_documents(mode="development")


def test_stable_validation_accepts_published_matrices() -> None:
    validate_compatibility_documents(mode="stable")


def test_matrix_entry_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        CompatibilityMatrixEntry.model_validate(
            {"release_id": "x", "platform": "linux", "extra": True}
        )


def test_matrix_entry_rejects_unknown_platform() -> None:
    with pytest.raises(ValidationError):
        CompatibilityMatrixEntry.model_validate({"release_id": "x", "platform": "freebsd"})


def test_validate_matrices_rejects_unknown_release() -> None:
    releases = [_Release("known", platforms=["linux"])]
    with pytest.raises(DomainError) as exc:
        validate_matrices(
            releases=releases,
            matrices={"create": [CompatibilityMatrixEntry(release_id="missing", platform="linux")]},
            harness_label="test",
        )
    assert exc.value.code is ErrorCode.PROVIDER_INCOMPATIBLE


def test_validate_matrices_rejects_duplicate_entries() -> None:
    releases = [_Release("known", platforms=["linux"])]
    entry = CompatibilityMatrixEntry(release_id="known", platform="linux")
    with pytest.raises(DomainError) as exc:
        validate_matrices(
            releases=releases,
            matrices={"create": [entry, entry]},
            harness_label="test",
        )
    assert "duplicate" in str(exc.value).lower()


def test_validate_matrices_rejects_platform_absent_from_release() -> None:
    releases = [_Release("known", platforms=["linux"])]
    with pytest.raises(DomainError) as exc:
        validate_matrices(
            releases=releases,
            matrices={"create": [CompatibilityMatrixEntry(release_id="known", platform="darwin")]},
            harness_label="test",
        )
    assert exc.value.code is ErrorCode.PROVIDER_INCOMPATIBLE


def test_validate_matrices_rejects_release_with_no_platforms() -> None:
    releases = [_Release("known", platforms=[])]
    with pytest.raises(DomainError) as exc:
        validate_matrices(
            releases=releases,
            matrices={"create": [CompatibilityMatrixEntry(release_id="known", platform="linux")]},
            harness_label="test",
        )
    assert exc.value.code is ErrorCode.PROVIDER_INCOMPATIBLE


def test_validate_matrices_rejects_resume_without_capability() -> None:
    releases = [_Release("known", platforms=["linux"], supports_resume=False)]
    with pytest.raises(DomainError) as exc:
        validate_matrices(
            releases=releases,
            matrices={"resume": [CompatibilityMatrixEntry(release_id="known", platform="linux")]},
            harness_label="test",
        )
    assert "resume" in str(exc.value).lower()


def test_membership_allows_empty_matrix_on_dev_version() -> None:
    assert_matrix_membership(
        release_id="known",
        platform="linux",
        matrix=[],
        mode="create",
        harness_label="test",
        package_version="2026.8.1.dev1",
    )


def test_membership_rejects_empty_matrix_on_stable_version() -> None:
    with pytest.raises(DomainError) as exc:
        assert_matrix_membership(
            release_id="known",
            platform="linux",
            matrix=[],
            mode="create",
            harness_label="test",
            package_version="2026.8.0",
        )
    assert exc.value.code is ErrorCode.PROVIDER_INCOMPATIBLE


def test_membership_requires_exact_platform() -> None:
    matrix = [CompatibilityMatrixEntry(release_id="known", platform="linux")]
    assert_matrix_membership(
        release_id="known",
        platform="linux",
        matrix=matrix,
        mode="create",
        harness_label="test",
        package_version="2026.8.0",
    )
    with pytest.raises(DomainError):
        assert_matrix_membership(
            release_id="known",
            platform="darwin",
            matrix=matrix,
            mode="create",
            harness_label="test",
            package_version="2026.8.0",
        )


def test_membership_bypass_for_fixtures() -> None:
    assert_matrix_membership(
        release_id="missing",
        platform="linux",
        matrix=[CompatibilityMatrixEntry(release_id="known", platform="linux")],
        mode="create",
        harness_label="test",
        package_version="2026.8.0",
        enforce_published=False,
    )


def test_is_development_version() -> None:
    assert is_development_version("2026.8.1.dev1") is True
    assert is_development_version("2026.8.0") is False


def test_matrix_entry_round_trip_json() -> None:
    raw = json.dumps([{"release_id": "known", "platform": "linux"}])
    entries = [CompatibilityMatrixEntry.model_validate(item) for item in json.loads(raw)]
    assert entries[0].release_id == "known"
    assert entries[0].platform == "linux"


def test_validate_matrices_rejects_steer_without_capability() -> None:
    releases = [_Release("known", platforms=["linux"], supports_steer=False)]
    with pytest.raises(DomainError) as exc:
        validate_matrices(
            releases=releases,
            matrices={"steer": [CompatibilityMatrixEntry(release_id="known", platform="linux")]},
            harness_label="test",
        )
    assert "supports_steer" in str(exc.value)


def test_markdown_includes_feature_matrices() -> None:
    md = render_supported_harnesses_markdown()
    assert "Interrupt" in md
    assert "Multi-interaction" in md
    assert "Nested" in md
    assert "### Published interrupt matrix" in md
    assert "### Published steer matrix" in md
    assert "### Published multi-interaction matrix" in md
    assert "### Published nested-activity matrix" in md

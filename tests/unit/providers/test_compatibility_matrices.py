"""Shared floor validation, comparators, advisory, and operation gating tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from talktoharnesses.domain.enums import ErrorCode
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.domain.models import HarnessCapabilities, HarnessKind
from talktoharnesses.providers.claude.compatibility import load_claude_compatibility
from talktoharnesses.providers.codex.compatibility import load_codex_compatibility
from talktoharnesses.providers.compatibility import (
    CompatibilityFloor,
    LatestVerified,
    ReleaseCapabilities,
    advisory_for_capabilities,
    compare_cursor_date,
    compare_dotted,
    enforce_operation,
    render_supported_harnesses_markdown,
    validate_compatibility_documents,
    validate_floor_document,
    version_advisory,
)
from talktoharnesses.providers.cursor.compatibility import load_cursor_compatibility
from talktoharnesses.providers.grok.compatibility import load_grok_compatibility
from talktoharnesses.providers.opencode.compatibility import load_opencode_compatibility
from talktoharnesses.providers.prime_agent.compatibility import load_prime_agent_compatibility


class _Doc:
    def __init__(
        self,
        *,
        adapter_version: str = "2026.8.5",
        floor: CompatibilityFloor,
        latest_verified: LatestVerified | None = None,
    ) -> None:
        self.adapter_version = adapter_version
        self.floor = floor
        self.latest_verified = latest_verified


def test_packaged_documents_load_with_floor() -> None:
    docs = (
        load_grok_compatibility(),
        load_cursor_compatibility(),
        load_codex_compatibility(),
        load_claude_compatibility(),
        load_opencode_compatibility(),
        load_prime_agent_compatibility(),
    )
    for doc in docs:
        assert doc.adapter_version == "2026.8.5"
        assert doc.floor.version
        assert "linux" in doc.floor.platforms
        assert doc.latest_verified is not None
        assert doc.floor.capabilities.supports_resume is True


def test_development_validation_allows_packaged_floors() -> None:
    validate_compatibility_documents(mode="development")


def test_stable_validation_accepts_published_floors() -> None:
    validate_compatibility_documents(mode="stable")


def test_compare_dotted() -> None:
    assert compare_dotted("1.0.5", "1.0.0") == 1
    assert compare_dotted("1.0.0", "1.0.5") == -1
    assert compare_dotted("1.0.5", "1.0.5") == 0
    assert compare_dotted("not-a-version", "1.0.0") is None


def test_compare_cursor_date_ignores_hash() -> None:
    assert compare_cursor_date("2026.08.11-e8db854", "2026.08.04") == 1
    assert compare_cursor_date("2026.08.03-deadbeef", "2026.08.04") == -1
    assert compare_cursor_date("2026.08.11-aaaa", "2026.08.11-bbbb") == 0
    assert compare_cursor_date("not-a-date", "2026.08.04") is None


def test_version_advisory_statuses() -> None:
    verified = version_advisory(
        probed="1.0.5", floor="1.0.0", latest_verified="1.0.5", compare=compare_dotted
    )
    assert verified.status == "verified"
    behind = version_advisory(
        probed="1.0.3", floor="1.0.0", latest_verified="1.0.5", compare=compare_dotted
    )
    assert behind.status == "behind_verified"
    ahead = version_advisory(
        probed="1.0.6", floor="1.0.0", latest_verified="1.0.5", compare=compare_dotted
    )
    assert ahead.status == "ahead_of_verified"
    unknown = version_advisory(
        probed="??", floor="1.0.0", latest_verified="1.0.5", compare=compare_dotted
    )
    assert unknown.status == "unknown"
    missing = version_advisory(
        probed="1.0.5", floor="1.0.0", latest_verified=None, compare=compare_dotted
    )
    assert missing.status == "unknown"


def test_advisory_for_grok_capabilities() -> None:
    caps = HarnessCapabilities(
        kind=HarnessKind.GROK,
        version="1.0.5 (5115b46bc9) [stable]",
        supports_resume=True,
    )
    advisory = advisory_for_capabilities(caps)
    assert advisory.status == "verified"
    assert advisory.probed_version == "1.0.5"
    assert advisory.floor_version == "1.0.0"


def test_enforce_operation_create_rejects_unpublished_platform() -> None:
    with pytest.raises(DomainError) as exc:
        enforce_operation(
            ReleaseCapabilities(supports_resume=True),
            mode="create",
            platforms=["linux"],
            harness_label="test",
            platform="darwin",
        )
    assert exc.value.code is ErrorCode.PROVIDER_INCOMPATIBLE


def test_enforce_operation_resume_requires_flag() -> None:
    caps = ReleaseCapabilities(supports_resume=False)
    with pytest.raises(DomainError) as exc:
        enforce_operation(
            caps,
            mode="resume",
            platforms=["linux"],
            harness_label="test",
            platform="linux",
        )
    assert "resume" in str(exc.value).lower()
    enforce_operation(
        ReleaseCapabilities(supports_resume=True),
        mode="resume",
        platforms=["linux"],
        harness_label="test",
        platform="linux",
    )


def test_enforce_operation_bypass() -> None:
    enforce_operation(
        ReleaseCapabilities(supports_resume=False),
        mode="resume",
        platforms=["linux"],
        harness_label="test",
        platform="linux",
        enforce=False,
    )


def test_floor_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        CompatibilityFloor.model_validate(
            {"version": "1.0.0", "platforms": ["linux"], "extra": True}
        )


def test_floor_rejects_unknown_platform() -> None:
    with pytest.raises(ValidationError):
        CompatibilityFloor.model_validate({"version": "1.0.0", "platforms": ["freebsd"]})


def test_validate_floor_rejects_empty_platforms() -> None:
    with pytest.raises(DomainError) as exc:
        validate_floor_document(
            _Doc(floor=CompatibilityFloor(version="1.0.0", platforms=[])),
            harness_label="test",
            compare=compare_dotted,
        )
    assert exc.value.code is ErrorCode.PROVIDER_INCOMPATIBLE


def test_validate_floor_rejects_malformed_version() -> None:
    with pytest.raises(DomainError) as exc:
        validate_floor_document(
            _Doc(floor=CompatibilityFloor(version="next", platforms=["linux"])),
            harness_label="test",
            compare=compare_dotted,
        )
    assert exc.value.code is ErrorCode.PROVIDER_INCOMPATIBLE


def test_validate_floor_rejects_latest_below_floor() -> None:
    with pytest.raises(DomainError) as exc:
        validate_floor_document(
            _Doc(
                floor=CompatibilityFloor(version="1.0.5", platforms=["linux"]),
                latest_verified=LatestVerified(version="1.0.0", platform="linux"),
            ),
            harness_label="test",
            compare=compare_dotted,
        )
    assert "latest_verified" in str(exc.value)


def test_validate_floor_rejects_latest_platform_absent() -> None:
    with pytest.raises(DomainError) as exc:
        validate_floor_document(
            _Doc(
                floor=CompatibilityFloor(version="1.0.0", platforms=["linux"]),
                latest_verified=LatestVerified(version="1.0.5", platform="darwin"),
            ),
            harness_label="test",
            compare=compare_dotted,
        )
    assert exc.value.code is ErrorCode.PROVIDER_INCOMPATIBLE


def test_markdown_includes_floors_not_matrices() -> None:
    md = render_supported_harnesses_markdown()
    assert "Interrupt" in md
    assert "Multi-interaction" in md
    assert "Nested" in md
    assert "### Adapter capabilities" in md
    assert "### Published interrupt matrix" not in md
    assert "### Published create matrix" not in md
    assert "Floor:" in md
    assert "Latest verified:" in md

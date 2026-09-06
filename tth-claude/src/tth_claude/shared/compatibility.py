"""Provider-neutral compatibility floor and operation gating."""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field
from tth_types.enums import ErrorCode, HarnessKind
from tth_types.errors import DomainError
from tth_types.harness import HarnessCapabilities, VersionAdvisory

_COMPAT = ConfigDict(extra="forbid", frozen=True)

PlatformName = Literal["linux", "darwin", "win32"]
MatrixMode = Literal[
    "create",
    "resume",
    "steer",
    "interrupt",
    "multi_interaction",
    "nested_activity",
]
VersionCompare = Callable[[str, str], int | None]
_KNOWN_PLATFORMS: frozenset[str] = frozenset({"linux", "darwin", "win32"})
MATRIX_MODES: tuple[MatrixMode, ...] = (
    "create",
    "resume",
    "steer",
    "interrupt",
    "multi_interaction",
    "nested_activity",
)
CAPABILITY_FLAG_FOR_MODE: dict[MatrixMode, str] = {
    "resume": "supports_resume",
    "steer": "supports_steer",
    "interrupt": "supports_interrupt",
    "multi_interaction": "supports_multi_interaction",
    "nested_activity": "supports_nested_activity",
}


class ReleaseCapabilities(BaseModel):
    """Adapter-owned capability flags copied onto probed identities."""

    model_config = _COMPAT

    supports_resume: bool = False
    supports_interrupt: bool = True
    supports_steer: bool = False
    supports_multi_interaction: bool = False
    supports_nested_activity: bool = False
    supports_mcp_servers: bool = False


class LatestVerified(BaseModel):
    """Last live-proven identity. Advisory only; not an allowlist."""

    model_config = _COMPAT

    version: str
    identity: str | None = None
    platform: PlatformName | None = None


class CompatibilityFloor(BaseModel):
    """Minimum identity the adapter will drive on published platforms."""

    model_config = _COMPAT

    version: str
    platforms: list[PlatformName] = Field(default_factory=list[PlatformName])
    capabilities: ReleaseCapabilities = Field(default_factory=ReleaseCapabilities)


class FloorDocument(Protocol):
    """Packaged JSON document shape used by validation."""

    @property
    def adapter_version(self) -> str: ...

    @property
    def floor(self) -> CompatibilityFloor: ...

    @property
    def latest_verified(self) -> LatestVerified | None: ...


def compare_dotted(left: str, right: str) -> int | None:
    """Compare dotted numeric versions (``1.0.5``, ``0.144.4``)."""
    parsed_left = _dotted_parts(left)
    parsed_right = _dotted_parts(right)
    if parsed_left is None or parsed_right is None:
        return None
    length = max(len(parsed_left), len(parsed_right))
    padded_left = parsed_left + (0,) * (length - len(parsed_left))
    padded_right = parsed_right + (0,) * (length - len(parsed_right))
    if padded_left < padded_right:
        return -1
    if padded_left > padded_right:
        return 1
    return 0


def compare_cursor_date(left: str, right: str) -> int | None:
    """Compare Cursor ``YYYY.MM.DD`` prefixes; ignore the trailing build hash."""
    parsed_left = _cursor_date_parts(left)
    parsed_right = _cursor_date_parts(right)
    if parsed_left is None or parsed_right is None:
        return None
    if parsed_left < parsed_right:
        return -1
    if parsed_left > parsed_right:
        return 1
    return 0


def version_advisory(
    *,
    probed: str,
    floor: str,
    latest_verified: str | None,
    compare: VersionCompare,
) -> VersionAdvisory:
    """Compare a probed identity to the floor and last live proof. Never raises."""
    if compare(probed, floor) is None:
        return VersionAdvisory(
            status="unknown",
            probed_version=probed,
            floor_version=floor,
            latest_verified=latest_verified,
        )
    if latest_verified is None or compare(probed, latest_verified) is None:
        return VersionAdvisory(
            status="unknown",
            probed_version=probed,
            floor_version=floor,
            latest_verified=latest_verified,
        )
    versus_latest = compare(probed, latest_verified)
    if versus_latest == 0:
        status: Literal["verified", "behind_verified", "ahead_of_verified", "unknown"] = "verified"
    elif versus_latest is not None and versus_latest < 0:
        status = "behind_verified"
    else:
        status = "ahead_of_verified"
    return VersionAdvisory(
        status=status,
        probed_version=probed,
        floor_version=floor,
        latest_verified=latest_verified,
    )


def comparable_probe_version(capabilities: HarnessCapabilities) -> str:
    """Extract the identity used for floor/advisory comparison."""
    raw = capabilities.version
    if capabilities.kind is HarnessKind.GROK:
        return raw.split()[0] if raw.split() else raw
    if capabilities.kind is HarnessKind.CURSOR:
        return raw.split("-", 1)[0]
    if capabilities.kind is HarnessKind.CLAUDE and "+cli-" in raw:
        return raw.split("+cli-", 1)[1]
    return raw


def assert_supported_platform(
    platform: str,
    platforms: Sequence[str],
    *,
    harness_label: str,
) -> None:
    """Reject unknown or unpublished platforms."""
    if platform not in _KNOWN_PLATFORMS:
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            f"unsupported platform for {harness_label}",
            details={"harness": harness_label, "platform": platform},
        )
    if platform not in platforms:
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            f"{harness_label} release not supported on this platform",
            details={
                "platform": platform,
                "supported_platforms": list(platforms),
            },
        )


def reject_below_floor(
    *,
    probed: str,
    floor: str,
    compare: VersionCompare,
    harness_label: str,
    details: dict[str, object],
) -> None:
    """Fail when the probed identity is older than the packaged floor."""
    versus = compare(probed, floor)
    if versus is None:
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            f"malformed {harness_label} version for floor comparison",
            details=details,
        )
    if versus < 0:
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            f"{harness_label} release is below the compatibility floor",
            details={**details, "floor_version": floor, "probed_version": probed},
        )


def enforce_operation(
    capabilities: ReleaseCapabilities,
    *,
    mode: MatrixMode,
    platforms: Sequence[str],
    harness_label: str,
    platform: str | None = None,
    enforce: bool = True,
) -> None:
    """Gate create/resume/steer/interrupt by floor platform and capability flags."""
    if not enforce:
        return
    assert_supported_platform(
        platform or sys.platform,
        platforms,
        harness_label=harness_label,
    )
    if mode == "create":
        return
    flag = CAPABILITY_FLAG_FOR_MODE.get(mode)
    if flag is None or bool(getattr(capabilities, flag)):
        return
    raise DomainError(
        ErrorCode.PROVIDER_INCOMPATIBLE,
        f"{harness_label} does not advertise {mode}",
        details={"harness": harness_label, "mode": mode},
    )


def validate_floor_document(
    doc: FloorDocument,
    *,
    harness_label: str,
    compare: VersionCompare,
) -> None:
    """Reject malformed floors or latest_verified entries below the floor."""
    if not doc.floor.version.strip():
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            "compatibility floor is missing a version",
            details={"harness": harness_label},
        )
    if compare(doc.floor.version, doc.floor.version) is None:
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            "compatibility floor version is malformed",
            details={"harness": harness_label, "floor_version": doc.floor.version},
        )
    if not doc.floor.platforms:
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            "compatibility floor requires at least one platform",
            details={"harness": harness_label},
        )
    for platform in doc.floor.platforms:
        if platform not in _KNOWN_PLATFORMS:
            raise DomainError(
                ErrorCode.PROVIDER_INCOMPATIBLE,
                "unsupported platform in compatibility floor",
                details={"harness": harness_label, "platform": platform},
            )
    latest = doc.latest_verified
    if latest is None:
        return
    versus = compare(latest.version, doc.floor.version)
    if versus is not None and versus < 0:
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            "latest_verified is below the compatibility floor",
            details={
                "harness": harness_label,
                "floor_version": doc.floor.version,
                "latest_verified": latest.version,
            },
        )
    if latest.platform is not None and latest.platform not in doc.floor.platforms:
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            "latest_verified platform is absent from the floor",
            details={
                "harness": harness_label,
                "platform": latest.platform,
                "supported_platforms": list(doc.floor.platforms),
            },
        )


def _dotted_parts(version: str) -> tuple[int, ...] | None:
    if not version or any(char.isspace() for char in version):
        return None
    parts = version.split(".")
    if not parts or any(not part.isdigit() for part in parts):
        return None
    return tuple(int(part) for part in parts)


def _cursor_date_parts(value: str) -> tuple[int, int, int] | None:
    date = value.split("-", 1)[0]
    parts = _dotted_parts(date)
    if parts is None or len(parts) != 3:
        return None
    year, month, day = parts
    if year < 2000 or not (1 <= month <= 12) or not (1 <= day <= 31):
        return None
    return year, month, day

"""Muse's release floor includes its numeric R-build identity."""

from __future__ import annotations

import json
import re
from functools import lru_cache
from importlib.resources import files

from pydantic import BaseModel
from tth_types.enums import ErrorCode
from tth_types.errors import DomainError

from tth_muse.shared.compatibility import (
    CompatibilityFloor,
    LatestVerified,
    validate_floor_document,
)

_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)-R(\d+)\.(\d+)")


def version_parts(version: str) -> tuple[int, ...]:
    match = _VERSION.fullmatch(version)
    if match is None:
        raise DomainError(ErrorCode.PROVIDER_INCOMPATIBLE, "Malformed Muse release identity")
    return tuple(int(part) for part in match.groups())


def compare_versions(left: str, right: str) -> int:
    a, b = version_parts(left), version_parts(right)
    return (a > b) - (a < b)


class MuseFloor(CompatibilityFloor):
    notes: str | None = None


class MuseCompatibilityDoc(BaseModel):
    adapter_version: str
    floor: MuseFloor
    latest_verified: LatestVerified | None = None


@lru_cache(maxsize=1)
def load_muse_compatibility() -> MuseCompatibilityDoc:
    doc = MuseCompatibilityDoc.model_validate(
        json.loads((files("tth_muse") / "data/compatibility/muse.json").read_text())
    )

    validate_floor_document(doc, harness_label="muse", compare=compare_versions)
    return doc

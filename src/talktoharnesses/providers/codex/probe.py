"""Codex SDK/runtime probe against the packaged compatibility source."""

from __future__ import annotations

import importlib.metadata
import sys
from typing import Any

from talktoharnesses.domain.enums import ErrorCode
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.domain.models import HarnessCapabilities, HarnessConfiguration
from talktoharnesses.providers.codex.compatibility import (
    CodexReleaseRecord,
    match_release,
)


def _import_openai_codex() -> Any:
    try:
        import openai_codex
    except ImportError as exc:
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            "openai-codex extra is not installed",
            details={"extra": "codex"},
        ) from exc
    return openai_codex


def _runtime_version() -> str:
    try:
        return importlib.metadata.version("openai-codex-cli-bin")
    except importlib.metadata.PackageNotFoundError:
        # Dev installs may expose the CLI via the openai-codex package alone.
        return importlib.metadata.version("openai-codex")


async def probe_codex(
    config: HarnessConfiguration,
) -> tuple[HarnessCapabilities, CodexReleaseRecord]:
    """Match installed SDK + runtime versions to a compatibility release."""
    del config  # Codex probe is SDK/runtime identity; no executable required.
    openai_codex = _import_openai_codex()
    sdk_version = str(getattr(openai_codex, "__version__", "") or "")
    if not sdk_version:
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            "openai_codex.__version__ missing",
        )
    runtime_version = _runtime_version()
    release = match_release(
        sdk_version=sdk_version,
        runtime_version=runtime_version,
        platform=sys.platform,
    )
    return release.to_harness_capabilities(), release

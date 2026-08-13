"""Codex SDK/runtime probe against the packaged compatibility source."""

from __future__ import annotations

import importlib.metadata
import sys
from typing import Any, cast

from talktoharnesses.domain.enums import ErrorCode
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.domain.models import (
    HarnessCapabilities,
    HarnessConfiguration,
    HarnessModelInfo,
)
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
    models = await _discover_models(openai_codex, config.working_directory)
    capabilities = release.to_harness_capabilities().model_copy(update={"models": models})
    return capabilities, release


async def _discover_models(
    openai_codex: Any, working_directory: str
) -> tuple[HarnessModelInfo, ...]:
    try:
        sdk_config = openai_codex.CodexConfig(cwd=working_directory)
        async with openai_codex.AsyncCodex(sdk_config) as client:
            response = await client.models()
    except Exception as exc:  # noqa: BLE001
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            "Codex model discovery failed",
        ) from exc
    data = getattr(response, "data", None)
    if not isinstance(data, list) or not data:
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            "Codex advertised no models",
        )
    models: list[HarnessModelInfo] = []
    for model in cast(list[object], data):
        model_id = getattr(model, "model", None)
        label = getattr(model, "display_name", None)
        if not isinstance(model_id, str) or not model_id:
            raise DomainError(
                ErrorCode.PROVIDER_INCOMPATIBLE,
                "malformed Codex model list",
            )
        models.append(
            HarnessModelInfo(
                id=model_id,
                label=label if isinstance(label, str) and label else None,
            )
        )
    return tuple(models)

"""Provider-neutral effort capability resolution and validation."""

from __future__ import annotations

from talktoharnesses.domain.enums import ErrorCode, HarnessKind
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.domain.models import (
    HarnessCapabilities,
    HarnessConfiguration,
    HarnessEffortInfo,
)


def effective_efforts(
    capabilities: HarnessCapabilities,
    model: str | None,
) -> tuple[HarnessEffortInfo, ...]:
    if model is not None:
        model_id = model
        if capabilities.kind is HarnessKind.CURSOR:
            model_id = model.partition("[")[0].strip()
        selected = next((item for item in capabilities.models if item.id == model_id), None)
        if selected is not None and selected.efforts is not None:
            return selected.efforts
    return capabilities.efforts


def validate_effort(
    configuration: HarnessConfiguration,
    capabilities: HarnessCapabilities,
) -> None:
    if configuration.effort is None:
        return
    advertised = effective_efforts(capabilities, configuration.model)
    if all(item.id != configuration.effort for item in advertised):
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            "harness does not support the requested effort",
            details={
                "effort": configuration.effort,
                "advertised_efforts": [item.id for item in advertised],
            },
        )

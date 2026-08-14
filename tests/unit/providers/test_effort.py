"""Provider-neutral effort capability resolution."""

import pytest

from talktoharnesses.domain.enums import HarnessKind
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.domain.models import (
    HarnessCapabilities,
    HarnessConfiguration,
    HarnessEffortInfo,
    HarnessModelInfo,
)
from talktoharnesses.providers.effort import effective_efforts, validate_effort


def test_model_efforts_override_global_and_empty_means_unsupported() -> None:
    global_efforts = (HarnessEffortInfo(id="medium", label="Medium"),)
    capabilities = HarnessCapabilities(
        kind=HarnessKind.OPENCODE,
        version="1",
        efforts=global_efforts,
        models=(
            HarnessModelInfo(id="inherits", efforts=None),
            HarnessModelInfo(id="unsupported", efforts=()),
        ),
    )

    assert effective_efforts(capabilities, "inherits") == global_efforts
    assert effective_efforts(capabilities, "unsupported") == ()
    with pytest.raises(DomainError, match="does not support"):
        validate_effort(
            HarnessConfiguration(
                kind=HarnessKind.OPENCODE,
                working_directory="/tmp",
                model="unsupported",
                effort="medium",
            ),
            capabilities,
        )


def test_cursor_model_selector_uses_base_model_efforts() -> None:
    efforts = (HarnessEffortInfo(id="high", label="High"),)
    capabilities = HarnessCapabilities(
        kind=HarnessKind.CURSOR,
        version="1",
        models=(HarnessModelInfo(id="gpt-5.6-sol", efforts=efforts),),
    )

    assert effective_efforts(capabilities, "gpt-5.6-sol[context=1m]") == efforts
    validate_effort(
        HarnessConfiguration(
            kind=HarnessKind.CURSOR,
            working_directory="/tmp",
            model="gpt-5.6-sol[context=1m]",
            effort="high",
        ),
        capabilities,
    )

"""Opt-in live Muse Code gate through TTH-managed Docker sandboxing.

Enable with ``TALKTOHARNESSES_LIVE_MUSE_SANDBOX=1``. The fixture
configures an isolated Docker sandbox; the create/resume journey then uses
TTH's official HTTP client. TTH owns credential seeding, routing, and split
authentication.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from tests.live.helpers import (
    LiveHttp,
    isolated_sandbox_environment,
    run_live_gate,
    unique_text_prompt,
)

from talktoharnesses.django.models import CommandRecord
from talktoharnesses.domain.enums import CommandKind, CommandStatus, HarnessKind
from talktoharnesses.domain.models import HarnessConfiguration

pytestmark = pytest.mark.skipif(
    os.environ.get("TALKTOHARNESSES_LIVE_MUSE_SANDBOX") != "1",
    reason=("set TALKTOHARNESSES_LIVE_MUSE_SANDBOX=1 to run the live Muse Code sandbox test"),
)


@pytest.fixture(autouse=True)
def muse_sandbox_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[None]:
    with isolated_sandbox_environment(
        monkeypatch,
        tmp_path_factory,
        kind=HarnessKind.MUSE,
        auth_environment_variable="TTH_SANDBOX_MUSE_AUTH_FILE",
        default_auth_path=Path(".config/muse/auth.json"),
        credential_environment_variable="META_API_KEY",
    ):
        yield


async def test_live_muse_through_tth_docker_sandbox(live_http: LiveHttp) -> None:
    await run_live_gate(
        live_http,
        configuration=HarnessConfiguration(
            kind=HarnessKind.MUSE,
            working_directory=str(live_http.workspace),
            model=os.environ.get("TALKTOHARNESSES_MUSE_MODEL"),
        ),
        min_create_interactions=0,
        min_resume_interactions=0,
        use_shell=False,
        prompt_fn=unique_text_prompt,
    )
    # A native turn can finish even when its approval decision failed. Counting
    # interaction requests alone must not certify successful answer delivery.
    assert not await CommandRecord.objects.filter(
        data__kind=CommandKind.ANSWER_INTERACTION.value,
        status=CommandStatus.OUTCOME_UNKNOWN.value,
    ).aexists(), "Muse approval delivery ended with an unknown native outcome"

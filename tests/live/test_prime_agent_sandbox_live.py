"""Opt-in live Prime Agent gate through TTH-managed Docker sandboxing.

Enable with ``TALKTOHARNESSES_LIVE_PRIME_AGENT_SANDBOX=1``. The fixture
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

from talktoharnesses.domain.enums import HarnessKind
from talktoharnesses.domain.models import HarnessConfiguration

pytestmark = pytest.mark.skipif(
    os.environ.get("TALKTOHARNESSES_LIVE_PRIME_AGENT_SANDBOX") != "1",
    reason=(
        "set TALKTOHARNESSES_LIVE_PRIME_AGENT_SANDBOX=1 to run the live Prime Agent sandbox test"
    ),
)


@pytest.fixture(autouse=True)
def prime_agent_sandbox_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[None]:
    with isolated_sandbox_environment(
        monkeypatch,
        tmp_path_factory,
        kind=HarnessKind.PRIME_AGENT,
        auth_environment_variable="TTH_SANDBOX_PRIME_AGENT_AUTH_FILE",
        default_auth_path=Path(".prime/config.json"),
    ):
        yield


async def test_live_prime_agent_through_tth_docker_sandbox(live_http: LiveHttp) -> None:
    await run_live_gate(
        live_http,
        configuration=HarnessConfiguration(
            kind=HarnessKind.PRIME_AGENT,
            working_directory=str(live_http.workspace),
            model=os.environ.get("TALKTOHARNESSES_PRIME_AGENT_MODEL"),
        ),
        min_create_interactions=0,
        min_resume_interactions=0,
        use_shell=False,
        prompt_fn=unique_text_prompt,
    )

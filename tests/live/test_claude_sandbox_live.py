"""Opt-in live Claude gate through TTH-managed Docker sandboxing.

Enable with ``TALKTOHARNESSES_LIVE_CLAUDE_SANDBOX=1``. The fixture configures
an isolated Docker sandbox; the create/resume journey then uses TTH's official
HTTP client. TTH owns credential seeding, routing, and split authentication.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from tests.live.helpers import (
    LiveHttp,
    assert_rtk_rewrite,
    isolated_sandbox_environment,
    run_live_gate,
    unique_prompt,
)

from talktoharnesses.domain.enums import HarnessKind
from talktoharnesses.domain.models import HarnessConfiguration

pytestmark = pytest.mark.skipif(
    os.environ.get("TALKTOHARNESSES_LIVE_CLAUDE_SANDBOX") != "1",
    reason="set TALKTOHARNESSES_LIVE_CLAUDE_SANDBOX=1 to run the live Claude sandbox test",
)


def _claude_prompt(prefix: str) -> str:
    return unique_prompt(prefix, mention_permission=False)


@pytest.fixture(autouse=True)
def claude_sandbox_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[None]:
    with isolated_sandbox_environment(
        monkeypatch,
        tmp_path_factory,
        kind=HarnessKind.CLAUDE,
        auth_environment_variable="TTH_SANDBOX_CLAUDE_AUTH_FILE",
        default_auth_path=Path(".claude/.credentials.json"),
        credential_environment_variable="ANTHROPIC_API_KEY",
    ):
        yield


async def test_live_claude_through_tth_docker_sandbox(live_http: LiveHttp) -> None:
    await run_live_gate(
        live_http,
        configuration=HarnessConfiguration(
            kind=HarnessKind.CLAUDE,
            working_directory=str(live_http.workspace),
        ),
        mention_permission=False,
        prompt_fn=_claude_prompt,
        after_create=assert_rtk_rewrite,
    )

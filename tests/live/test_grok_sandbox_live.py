"""Opt-in live Grok gate through TTH-managed Docker sandboxing.

Enable with ``TALKTOHARNESSES_LIVE_GROK_SANDBOX=1``. The fixture configures an
isolated Docker sandbox; the create/resume journey then uses TTH's official
HTTP client. TTH owns credential seeding, routing, and split authentication.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from tests.live.helpers import LiveHttp, isolated_sandbox_environment, run_live_gate

from talktoharnesses.domain.enums import HarnessKind
from talktoharnesses.domain.models import HarnessConfiguration

pytestmark = pytest.mark.skipif(
    os.environ.get("TALKTOHARNESSES_LIVE_GROK_SANDBOX") != "1",
    reason="set TALKTOHARNESSES_LIVE_GROK_SANDBOX=1 to run the live Grok sandbox test",
)


@pytest.fixture(autouse=True)
def grok_sandbox_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[None]:
    with isolated_sandbox_environment(
        monkeypatch,
        tmp_path_factory,
        kind=HarnessKind.GROK,
        auth_environment_variable="TTH_SANDBOX_GROK_AUTH_FILE",
        default_auth_path=Path(".grok/auth.json"),
        credential_environment_variable="XAI_API_KEY",
    ):
        yield


async def test_live_grok_through_tth_docker_sandbox(live_http: LiveHttp) -> None:
    await run_live_gate(
        live_http,
        configuration=HarnessConfiguration(
            kind=HarnessKind.GROK,
            working_directory=str(live_http.workspace),
        ),
    )

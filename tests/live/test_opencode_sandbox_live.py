"""Opt-in live OpenCode gate through TTH-managed Docker sandboxing.

Enable with ``TALKTOHARNESSES_LIVE_OPENCODE_SANDBOX=1``. The fixture configures
an isolated Docker sandbox; the create/resume journey then uses TTH's official
HTTP client. TTH owns credential seeding, routing, and split authentication.
The seeded credential is the file ``opencode auth login`` writes on the host.
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
    os.environ.get("TALKTOHARNESSES_LIVE_OPENCODE_SANDBOX") != "1",
    reason="set TALKTOHARNESSES_LIVE_OPENCODE_SANDBOX=1 to run the live OpenCode sandbox test",
)


@pytest.fixture(autouse=True)
def opencode_sandbox_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[None]:
    with isolated_sandbox_environment(
        monkeypatch,
        tmp_path_factory,
        kind=HarnessKind.OPENCODE,
        auth_environment_variable="TTH_SANDBOX_OPENCODE_AUTH_FILE",
        default_auth_path=Path(".local/share/opencode/auth.json"),
        credential_environment_variable="OPENCODE_API_KEY",
    ):
        yield


async def test_live_opencode_through_tth_docker_sandbox(live_http: LiveHttp) -> None:
    # Force shell tool approvals through the broker for this disposable workspace.
    (live_http.workspace / "opencode.json").write_text(
        '{\n  "permission": {\n    "bash": "ask"\n  }\n}\n',
        encoding="utf-8",
    )
    await run_live_gate(
        live_http,
        configuration=HarnessConfiguration(
            kind=HarnessKind.OPENCODE,
            working_directory=str(live_http.workspace),
            model=os.environ.get("TALKTOHARNESSES_OPENCODE_MODEL", "opencode/big-pickle"),
        ),
        min_resume_interactions=0,
    )

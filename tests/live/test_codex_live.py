"""Opt-in live Codex create/resume/interaction tests.

Enable with TALKTOHARNESSES_LIVE_CODEX=1.
When enabled, missing SDK/auth/version is a failure rather than a skip.

When the live stream surfaces approvals, they must resolve through the
broker-compatible HTTP interaction path (no private SDK attrs, no silent
auto-approval) before create/resume matrix rows are published.
"""

from __future__ import annotations

import os

import pytest
from tests.live.helpers import LiveHttp, run_live_gate

from talktoharnesses.domain.enums import HarnessKind
from talktoharnesses.domain.models import HarnessConfiguration
from talktoharnesses.providers.codex.compatibility import load_codex_compatibility

pytestmark = pytest.mark.skipif(
    os.environ.get("TALKTOHARNESSES_LIVE_CODEX") != "1",
    reason="set TALKTOHARNESSES_LIVE_CODEX=1 to run live Codex tests",
)


async def test_live_codex_create_resume_interaction(live_http: LiveHttp) -> None:
    await run_live_gate(
        live_http,
        configuration=HarnessConfiguration(
            kind=HarnessKind.CODEX,
            working_directory=str(live_http.workspace),
            mode="workspace_write",
        ),
        releases=load_codex_compatibility().releases,
    )

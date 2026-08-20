"""Opt-in live Grok create/resume/interaction tests.

Enable with TALKTOHARNESSES_LIVE_GROK=1 and TALKTOHARNESSES_GROK_EXECUTABLE.
When enabled, missing auth/binary/version is a failure rather than a skip.
"""

from __future__ import annotations

import os

import pytest
from tests.live.helpers import LiveHttp, require_executable, run_live_gate

from talktoharnesses.domain.enums import HarnessKind
from talktoharnesses.domain.models import HarnessConfiguration

pytestmark = pytest.mark.skipif(
    os.environ.get("TALKTOHARNESSES_LIVE_GROK") != "1",
    reason="set TALKTOHARNESSES_LIVE_GROK=1 to run live Grok tests",
)


async def test_live_grok_create_resume_interaction(live_http: LiveHttp) -> None:
    await run_live_gate(
        live_http,
        configuration=HarnessConfiguration(
            kind=HarnessKind.GROK,
            executable_path=require_executable("TALKTOHARNESSES_GROK_EXECUTABLE"),
            working_directory=str(live_http.workspace),
        ),
    )

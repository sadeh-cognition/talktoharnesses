"""Opt-in live Claude create/resume/interaction tests.

Enable with TALKTOHARNESSES_LIVE_CLAUDE=1.
When enabled, missing SDK/auth/CLI/version is a failure rather than a skip.
"""

from __future__ import annotations

import os

import pytest
from tests.live.helpers import LiveHttp, run_live_gate, unique_prompt

from talktoharnesses.domain.enums import HarnessKind
from talktoharnesses.domain.models import HarnessConfiguration

pytestmark = pytest.mark.skipif(
    os.environ.get("TALKTOHARNESSES_LIVE_CLAUDE") != "1",
    reason="set TALKTOHARNESSES_LIVE_CLAUDE=1 to run live Claude tests",
)


def _claude_prompt(prefix: str) -> str:
    return unique_prompt(prefix, mention_permission=False)


async def test_live_claude_create_resume_interaction(live_http: LiveHttp) -> None:
    await run_live_gate(
        live_http,
        configuration=HarnessConfiguration(
            kind=HarnessKind.CLAUDE,
            working_directory=str(live_http.workspace),
        ),
        mention_permission=False,
        prompt_fn=_claude_prompt,
    )

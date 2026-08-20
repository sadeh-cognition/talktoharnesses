"""Opt-in live Prime Agent create/resume gate.

Enable with TALKTOHARNESSES_LIVE_PRIME_AGENT=1 and
TALKTOHARNESSES_PRIME_AGENT_EXECUTABLE. When enabled, missing auth, binary, or
the exact published version is a failure rather than a skip.
"""

from __future__ import annotations

import os

import pytest
from tests.live.helpers import LiveHttp, require_executable, run_live_gate, unique_text_prompt

from talktoharnesses.domain.enums import HarnessKind
from talktoharnesses.domain.models import HarnessConfiguration
from talktoharnesses.providers.prime_agent.compatibility import load_prime_agent_compatibility

pytestmark = pytest.mark.skipif(
    os.environ.get("TALKTOHARNESSES_LIVE_PRIME_AGENT") != "1",
    reason="set TALKTOHARNESSES_LIVE_PRIME_AGENT=1 to run live Prime Agent tests",
)


async def test_live_prime_agent_create_resume(live_http: LiveHttp) -> None:
    await run_live_gate(
        live_http,
        configuration=HarnessConfiguration(
            kind=HarnessKind.PRIME_AGENT,
            executable_path=require_executable("TALKTOHARNESSES_PRIME_AGENT_EXECUTABLE"),
            working_directory=str(live_http.workspace),
            model=os.environ.get("TALKTOHARNESSES_PRIME_AGENT_MODEL"),
        ),
        releases=load_prime_agent_compatibility().releases,
        expected_release_id="prime-agent-0.7.1",
        min_create_interactions=0,
        min_resume_interactions=0,
        use_shell=False,
        prompt_fn=unique_text_prompt,
    )

"""Opt-in live OpenCode create/resume/interaction tests.

Enable with TALKTOHARNESSES_LIVE_OPENCODE=1 and TALKTOHARNESSES_OPENCODE_EXECUTABLE.
When enabled, missing auth/binary/version is a failure rather than a skip.
"""

from __future__ import annotations

import os

import pytest
from tests.live.helpers import LiveHttp, require_executable, run_live_gate

from talktoharnesses.domain.enums import HarnessKind
from talktoharnesses.domain.models import HarnessConfiguration
from talktoharnesses.providers.opencode.compatibility import load_opencode_compatibility

pytestmark = pytest.mark.skipif(
    os.environ.get("TALKTOHARNESSES_LIVE_OPENCODE") != "1",
    reason="set TALKTOHARNESSES_LIVE_OPENCODE=1 to run live OpenCode tests",
)


async def test_live_opencode_create_resume_interaction(live_http: LiveHttp) -> None:
    # Force shell tool approvals through the broker for this disposable workspace.
    (live_http.workspace / "opencode.json").write_text(
        '{\n  "permission": {\n    "bash": "ask"\n  }\n}\n',
        encoding="utf-8",
    )
    await run_live_gate(
        live_http,
        configuration=HarnessConfiguration(
            kind=HarnessKind.OPENCODE,
            executable_path=require_executable("TALKTOHARNESSES_OPENCODE_EXECUTABLE"),
            working_directory=str(live_http.workspace),
            model=os.environ.get("TALKTOHARNESSES_OPENCODE_MODEL", "opencode/big-pickle"),
        ),
        releases=load_opencode_compatibility().releases,
        min_resume_interactions=0,
    )

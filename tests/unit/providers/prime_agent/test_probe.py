"""Prime Agent executable probing."""

from pathlib import Path

import pytest

from talktoharnesses.domain.enums import HarnessKind
from talktoharnesses.domain.models import HarnessConfiguration
from talktoharnesses.providers.prime_agent.probe import probe_prime_agent


@pytest.mark.asyncio
async def test_probe_accepts_version_on_stderr(tmp_path: Path) -> None:
    executable = tmp_path / "prime-agent"
    executable.write_text("#!/bin/sh\necho 0.7.1 >&2\n", encoding="utf-8")
    executable.chmod(0o755)
    capabilities, release = await probe_prime_agent(
        HarnessConfiguration(
            kind=HarnessKind.PRIME_AGENT,
            executable_path=str(executable),
            working_directory=str(tmp_path),
        )
    )
    assert release.id == "prime-agent-0.7.1"
    assert capabilities.version == "0.7.1"

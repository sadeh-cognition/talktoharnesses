"""Prime Agent executable probing."""

from pathlib import Path

import pytest

from talktoharnesses.domain.enums import ErrorCode, HarnessKind
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.domain.models import HarnessConfiguration
from talktoharnesses.providers.prime_agent import probe as probe_mod
from talktoharnesses.providers.prime_agent.probe import probe_prime_agent


@pytest.mark.parametrize(
    "output",
    (
        "",
        "provider model context max-out thinking images",
        "provider model context max-out thinking images\nprime model too-few",
    ),
)
def test_prime_agent_model_list_rejects_malformed_output(output: str) -> None:
    with pytest.raises(DomainError) as exc:
        probe_mod._parse_models(output)  # pyright: ignore[reportPrivateUsage]
    assert exc.value.code is ErrorCode.PROVIDER_INCOMPATIBLE


@pytest.mark.asyncio
async def test_probe_accepts_version_on_stderr(tmp_path: Path) -> None:
    executable = tmp_path / "prime-agent"
    executable.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then\n'
        "  echo 0.7.1 >&2\n"
        "else\n"
        "  echo 'provider model context max-out thinking images'\n"
        "  echo 'prime-inference openai/gpt-5.6-sol 1.1M 128K yes yes'\n"
        "fi\n",
        encoding="utf-8",
    )
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
    assert capabilities.models[0].id == "prime-inference/openai/gpt-5.6-sol"

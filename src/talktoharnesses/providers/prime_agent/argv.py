"""Prime Agent RPC launch arguments."""

from __future__ import annotations

from talktoharnesses.domain.enums import ErrorCode
from talktoharnesses.domain.errors import DomainError

THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")


def build_prime_agent_argv(*, model: str | None, thinking: str | None) -> tuple[str, ...]:
    argv = ["--mode", "rpc"]
    if model:
        argv.extend(("--model", model))
    if thinking:
        if thinking not in THINKING_LEVELS:
            raise DomainError(
                ErrorCode.PROVIDER_INCOMPATIBLE,
                "prime agent mode must be a supported thinking level",
                details={"mode": thinking, "supported_modes": list(THINKING_LEVELS)},
            )
        argv.extend(("--thinking", thinking))
    return tuple(argv)

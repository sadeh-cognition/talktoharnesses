"""Prime Agent RPC launch arguments."""

from __future__ import annotations

from talktoharnesses.domain.enums import ErrorCode
from talktoharnesses.domain.errors import DomainError

THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")


def build_prime_agent_argv(*, model: str | None, effort: str | None) -> tuple[str, ...]:
    argv = ["--mode", "rpc"]
    if model:
        argv.extend(("--model", model))
    if effort:
        if effort not in THINKING_LEVELS:
            raise DomainError(
                ErrorCode.PROVIDER_INCOMPATIBLE,
                "prime agent effort must be a supported thinking level",
                details={"effort": effort, "supported_efforts": list(THINKING_LEVELS)},
            )
        argv.extend(("--thinking", effort))
    return tuple(argv)

"""Frozen runtime timer policy with Phase 3 defaults."""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import TypeVar

from pydantic import BaseModel, Field

from talktoharnesses.domain._base import FROZEN

IDLE_REAP_ENV = "TTH_RUNTIME_IDLE_REAP_SECONDS"
MAX_RUNTIMES_ENV = "TTH_RUNTIME_MAX_RUNTIMES"

_Number = TypeVar("_Number", int, float)


def _positive(name: str, raw: str, parse: Callable[[str], _Number]) -> _Number:
    try:
        value = parse(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive number, got {raw!r}") from exc
    if value <= 0:
        raise ValueError(f"{name} must be a positive number, got {raw!r}")
    return value


class RuntimePolicy(BaseModel):
    """Timeouts and budgets for process and session supervision (seconds)."""

    model_config = FROZEN

    creation_timeout: float = Field(default=10.0, gt=0)
    start_resume_timeout: float = Field(default=60.0, gt=0)
    # Idle runtimes are closed after this long; the next turn resumes natively.
    idle_reap: float = Field(default=5 * 60, gt=0)
    silence_warning: float = Field(default=2 * 60, gt=0)
    interrupt_timeout: float = Field(default=5.0, gt=0)
    graceful_close_timeout: float = Field(default=5.0, gt=0)
    terminate_escalation: float = Field(default=2.0, gt=0)
    shutdown_budget: float = Field(default=10.0, gt=0)
    lease_duration: float = Field(default=30.0, gt=0)
    lease_renewal_interval: float = Field(default=10.0, gt=0)
    # Live runtimes plus transient switch/rotation candidates.
    max_runtimes: int = Field(default=20, gt=0)

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> RuntimePolicy:
        """Defaults overridden by ``TTH_RUNTIME_*``; unset or empty keeps the default."""
        env = dict(os.environ) if environ is None else environ
        overrides: dict[str, object] = {}
        if raw := env.get(IDLE_REAP_ENV):
            overrides["idle_reap"] = _positive(IDLE_REAP_ENV, raw, float)
        if raw := env.get(MAX_RUNTIMES_ENV):
            overrides["max_runtimes"] = _positive(MAX_RUNTIMES_ENV, raw, int)
        return cls.model_validate(overrides)

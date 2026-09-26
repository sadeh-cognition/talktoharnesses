"""Frozen runtime timer policy with Phase 3 defaults."""

from __future__ import annotations

from pydantic import BaseModel, Field
from tth_types.base import FROZEN


class RuntimePolicy(BaseModel):
    """Timeouts and budgets for process and session supervision (seconds)."""

    model_config = FROZEN

    creation_timeout: float = Field(default=10.0, gt=0)
    start_resume_timeout: float = Field(default=60.0, gt=0)
    idle_reap: float = Field(default=15 * 60, gt=0)
    silence_warning: float = Field(default=2 * 60, gt=0)
    interrupt_timeout: float = Field(default=5.0, gt=0)
    graceful_close_timeout: float = Field(default=5.0, gt=0)
    terminate_escalation: float = Field(default=2.0, gt=0)
    shutdown_budget: float = Field(default=10.0, gt=0)
    lease_duration: float = Field(default=30.0, gt=0)
    lease_renewal_interval: float = Field(default=10.0, gt=0)
    # Live runtimes plus transient switch/rotation candidates.
    max_runtimes: int = Field(default=20, gt=0)
    # How long a split session whose proxy stream dropped waits for the proxy to
    # reattach before it is closed; 0 closes it at once.
    detach_grace: float = Field(default=120.0, ge=0)

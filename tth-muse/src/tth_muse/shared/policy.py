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
    # Muse push-subscription watchdog: after this many seconds without a
    # frame during an active turn, page the session view to check whether
    # the host stopped pushing (it does, silently, after a few hundred
    # events); a dead subscription is re-established and the gap replayed.
    push_stall_probe: float = Field(default=20.0, gt=0)
    push_poll_interval: float = Field(default=2.0, gt=0)
    push_recovery_page_limit: int = Field(default=100, gt=0)
    push_recovery_max_pages: int = Field(default=20, gt=0)

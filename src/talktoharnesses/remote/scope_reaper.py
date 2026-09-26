"""Reclaim the Docker resources of policy sandbox scopes nobody uses any more.

Every scope keeps two tiers of state with different lifetimes:

- Containers and the internal network are disposable. Preparing the scope
  again recreates them, so they are removed once the scope has been idle for
  a while, or straight away when they are dead (a Docker Desktop restart can
  leave them unstartable).
- The ``-home``/``-data`` volumes, the state directory, and the sandbox row
  hold harness sessions and caches, so going back to a finished run still
  resumes natively. They are purged only when a mounted host path no longer
  exists (its worktree was deleted) or after a long idle period.

Only scopes whose state directory lives under this process's state root are
considered (see :meth:`ScopedSandboxManager.owned_scopes`); legacy per-kind
``tth-<kind>`` sandboxes and other roots' scopes are out of reach.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import BaseModel, Field, model_validator

from talktoharnesses.domain._base import FROZEN
from talktoharnesses.remote.isolated_sandbox import CA_MOUNT_PATHS, SandboxMounts
from talktoharnesses.remote.scope_layout import SCOPE_LABEL, ScopeLayout
from talktoharnesses.remote.scoped_sandboxes import ScopedSandboxManager

logger = logging.getLogger(__name__)

ENABLED_ENV = "TTH_SANDBOX_REAPER"
_SECONDS_ENV = {
    "interval": "TTH_SANDBOX_REAP_INTERVAL_SECONDS",
    "container_idle": "TTH_SANDBOX_CONTAINER_IDLE_SECONDS",
    "purge_idle": "TTH_SANDBOX_PURGE_IDLE_SECONDS",
}
_ALIVE = frozenset({"running", "restarting"})

Action = Literal["stop", "purge"]


class ScopeReaperPolicy(BaseModel):
    """When scope resources are reclaimed (seconds)."""

    model_config = FROZEN

    # Off keeps reclaiming off, but passes still record this process's use.
    enabled: bool = True
    interval: float = Field(default=10 * 60, gt=0)
    startup_delay: float = Field(default=30.0, ge=0)
    # Containers and network of a scope unused for this long are removed.
    container_idle: float = Field(default=24 * 60 * 60, gt=0)
    # Volumes, state, and row of a scope unused for this long are purged;
    # 0 keeps them until a mounted path disappears.
    purge_idle: float = Field(default=90 * 24 * 60 * 60, ge=0)

    @model_validator(mode="after")
    def _heartbeat_within_idle(self) -> Self:
        # Each pass marks this process's scopes used; other processes judge
        # them by that mark, so it must be renewed before they count as idle.
        if self.interval >= self.container_idle:
            raise ValueError("the reap interval must be shorter than the container idle period")
        return self

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> ScopeReaperPolicy:
        """Defaults overridden by ``TTH_SANDBOX_*``; unset or empty keeps the default."""
        env = dict(os.environ) if environ is None else environ
        overrides: dict[str, object] = {"enabled": env.get(ENABLED_ENV) != "0"}
        for field, name in _SECONDS_ENV.items():
            if raw := env.get(name):
                try:
                    overrides[field] = float(raw)
                except ValueError as exc:
                    raise ValueError(f"{name} must be a number of seconds, got {raw!r}") from exc
        return cls.model_validate(overrides)


@dataclass(frozen=True)
class ScopeFacts:
    """What one pass observed about a scope."""

    name: str
    last_used: float
    missing_mount: bool
    # Containers or the network still exist.
    stoppable: bool
    # The split or gateway container is missing or not running.
    dead: bool


@dataclass(frozen=True)
class ReapReport:
    stopped: tuple[str, ...] = ()
    purged: tuple[str, ...] = ()


def decide(scope: ScopeFacts, *, now: float, policy: ScopeReaperPolicy) -> Action | None:
    """What to reclaim from a scope nobody in this process is using."""
    if scope.missing_mount:
        return "purge"
    idle = now - scope.last_used
    if policy.purge_idle and idle >= policy.purge_idle:
        return "purge"
    if scope.stoppable and (scope.dead or idle >= policy.container_idle):
        return "stop"
    return None


class ScopeReaper:
    """Periodically reclaims idle and dead scopes of one scoped sandbox manager."""

    def __init__(
        self,
        sandboxes: ScopedSandboxManager,
        policy: ScopeReaperPolicy,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.sandboxes = sandboxes
        self.policy = policy
        self._clock = clock
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="tth-sandbox-scope-reaper")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _loop(self) -> None:
        await asyncio.sleep(self.policy.startup_delay)
        while True:
            try:
                await self.tick()
            except Exception:
                logger.exception("sandbox scope reaping failed")
            await asyncio.sleep(self.policy.interval)

    async def tick(self) -> ReapReport:
        """Record this process's use of its scopes, then reclaim unless disabled."""
        await self.sandboxes.touch_in_use()
        return await self.reap_once() if self.policy.enabled else ReapReport()

    async def reap_once(self) -> ReapReport:
        try:
            client = await asyncio.to_thread(self.sandboxes.client_factory)
        except Exception as exc:
            logger.warning("sandbox scope reaping skipped: docker is unreachable: %s", exc)
            return ReapReport()
        scopes = await asyncio.to_thread(self._survey, client)
        now = self._clock()
        stopped: list[str] = []
        purged: list[str] = []
        for scope in scopes:
            action = decide(scope, now=now, policy=self.policy)
            if action is None:
                continue
            try:
                # Refused while this process uses the scope.
                if not await self.sandboxes.reclaim(scope.name, purge=action == "purge"):
                    continue
            except Exception:
                logger.warning("could not reclaim sandbox scope %s", scope.name, exc_info=True)
                continue
            (purged if action == "purge" else stopped).append(scope.name)
        report = ReapReport(stopped=tuple(stopped), purged=tuple(purged))
        if stopped or purged:
            logger.info("reclaimed sandbox scopes: stopped=%d purged=%d", len(stopped), len(purged))
        return report

    def _survey(self, client: Any) -> list[ScopeFacts]:
        """Blocking; gather the facts about every scope this process owns."""
        statuses = {
            container.name: container.status
            for container in client.containers.list(all=True, filters={"label": SCOPE_LABEL})
        }
        networks = {
            network.name for network in client.networks.list(filters={"label": SCOPE_LABEL})
        }
        facts: list[ScopeFacts] = []
        for layout in self.sandboxes.owned_scopes():
            containers = (statuses.get(layout.name), statuses.get(layout.gateway))
            facts.append(
                ScopeFacts(
                    name=layout.name,
                    last_used=_last_used(layout),
                    missing_mount=_missing_mount(layout),
                    stoppable=layout.network in networks or containers != (None, None),
                    dead=any(status not in _ALIVE for status in containers),
                )
            )
        return facts


def _last_used(layout: ScopeLayout) -> float:
    """Newest use: an adapter binding or leaving, or a preparation (via the directory)."""
    times: list[float] = []
    for path in (layout.last_used_file, layout.state):
        with contextlib.suppress(OSError):
            times.append(path.stat().st_mtime)
    return max(times, default=0.0)


def _missing_mount(layout: ScopeLayout) -> bool:
    """True when a host path the scope mounts no longer exists."""
    try:
        mounts = SandboxMounts.model_validate_json(layout.mounts_file.read_text())
    except (OSError, ValueError):
        return False
    return any(not Path(path).exists() for path in mounts.sources if path not in CA_MOUNT_PATHS)

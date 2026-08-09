"""Process-local recent-probe readiness monitor (Phase 9 WP5)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from uuid import UUID

from talktoharnesses.application.persistence import Persistence
from talktoharnesses.domain.enums import ErrorCode
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.domain.models import HarnessProjection
from talktoharnesses.providers.registry import AdapterRegistry

logger = logging.getLogger(__name__)

PROBE_FRESHNESS = timedelta(minutes=5)
_REFRESH_LEAD = timedelta(seconds=30)
_RETRY_WHEN_STALE = timedelta(seconds=30)


class ReadinessProbeMonitor:
    """Probe configured harnesses and cache a fixed freshness deadline."""

    def __init__(
        self,
        persistence: Persistence,
        registry: AdapterRegistry,
        clock: Callable[[], datetime],
    ) -> None:
        self._persistence = persistence
        self._registry = registry
        self._clock = clock
        self._fresh_until: datetime | None = None
        self._successful_harness_id: UUID | None = None
        self._task: asyncio.Task[None] | None = None
        self._stopped = False

    def is_fresh(self, now: datetime) -> bool:
        return self._fresh_until is not None and now < self._fresh_until

    async def has_fresh_probe(self, now: datetime | None = None) -> bool:
        current = now if now is not None else self._clock()
        if self.is_fresh(current):
            return True
        return await self._persistence.has_fresh_harness_probe(
            now=current,
            max_age_seconds=int(PROBE_FRESHNESS.total_seconds()),
        )

    def notify_success(self, probed_at: datetime, harness_id: UUID | None = None) -> None:
        self._fresh_until = probed_at + PROBE_FRESHNESS
        if harness_id is not None:
            self._successful_harness_id = harness_id

    async def start(self) -> None:
        """Run the initial probe pass, then keep freshness refreshed in the background."""
        if self._stopped:
            self._stopped = False
        await self._probe_until_success(prefer_harness_id=None)
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._refresh_loop(), name="readiness-probe")

    async def shutdown(self, deadline: float | None = None) -> None:
        """Cancel the background refresh loop under the shared shutdown deadline."""
        self._stopped = True
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        remaining: float | None = None
        if deadline is not None:
            remaining = max(0.0, deadline - time.monotonic())
        with contextlib.suppress(asyncio.CancelledError, Exception):
            if remaining is None:
                await task
            else:
                await asyncio.wait_for(task, timeout=remaining)

    async def _refresh_loop(self) -> None:
        while not self._stopped:
            try:
                await self._sleep_until_refresh_due()
                if self._stopped:
                    return
                await self._refresh_cycle()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "readiness probe refresh failed code=%s",
                    ErrorCode.PROVIDER_INCOMPATIBLE.value,
                )
                await asyncio.sleep(_RETRY_WHEN_STALE.total_seconds())

    async def _sleep_until_refresh_due(self) -> None:
        now = self._clock()
        if self._fresh_until is None:
            await asyncio.sleep(_RETRY_WHEN_STALE.total_seconds())
            return
        due_at = self._fresh_until - _REFRESH_LEAD
        delay = (due_at - now).total_seconds()
        if delay > 0:
            await asyncio.sleep(delay)

    async def _refresh_cycle(self) -> None:
        prefer = self._successful_harness_id
        if prefer is not None:
            harnesses = await self._persistence.list_configured_harnesses_for_readiness()
            preferred = next((h for h in harnesses if h.id == prefer), None)
            if preferred is not None and await self._probe_one(preferred):
                return
            # Preferred harness failed — try the remaining configured set.
            await self._probe_until_success(
                prefer_harness_id=prefer,
                skip_preferred=True,
                harnesses=harnesses,
            )
            return
        await self._probe_until_success(prefer_harness_id=None)

    async def _probe_until_success(
        self,
        *,
        prefer_harness_id: UUID | None,
        skip_preferred: bool = False,
        harnesses: Sequence[HarnessProjection] | None = None,
    ) -> bool:
        configured = (
            list(harnesses)
            if harnesses is not None
            else list(await self._persistence.list_configured_harnesses_for_readiness())
        )
        configured.sort(key=lambda h: h.id)
        if prefer_harness_id is not None and not skip_preferred:
            preferred = next((h for h in configured if h.id == prefer_harness_id), None)
            if preferred is not None:
                configured = [preferred] + [h for h in configured if h.id != prefer_harness_id]
        elif skip_preferred and prefer_harness_id is not None:
            configured = [h for h in configured if h.id != prefer_harness_id]
        for harness in configured:
            if await self._probe_one(harness):
                return True
        return False

    async def _probe_one(self, harness: HarnessProjection) -> bool:
        try:
            adapter = self._registry.create(harness.kind)
            capabilities = await adapter.probe(harness.configuration)
            probed_at = self._clock()
            await self._persistence.save_harness_probe(
                harness.id,
                harness.owner_id,
                capabilities,
                probed_at=probed_at,
            )
            self.notify_success(probed_at, harness.id)
            return True
        except DomainError as exc:
            logger.warning("readiness probe failed code=%s", exc.code.value)
            return False
        except Exception:
            logger.warning(
                "readiness probe failed code=%s",
                ErrorCode.PROVIDER_INCOMPATIBLE.value,
            )
            return False

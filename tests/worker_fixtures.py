"""Readiness setup for tests that drive the command worker themselves."""

from __future__ import annotations

from unittest.mock import Mock

from talktoharnesses.application.service import TalkToHarnessesService


def mark_worker_ready(service: TalkToHarnessesService) -> None:
    """Report the command worker ready without starting it.

    The service refuses new commands until its worker is ready for work; tests
    that never call ``start`` use this so it accepts them.
    """
    coordinator = service.coordinator
    coordinator._lease_healthy = True  # pyright: ignore[reportPrivateUsage]
    coordinator._heartbeat_healthy = True  # pyright: ignore[reportPrivateUsage]
    coordinator._initial_recovery_complete = True  # pyright: ignore[reportPrivateUsage]
    coordinator._draining = False  # pyright: ignore[reportPrivateUsage]
    coordinator._claims_healthy = True  # pyright: ignore[reportPrivateUsage]
    service.processor._running = True  # pyright: ignore[reportPrivateUsage]
    service.processor._claim_task = Mock(done=Mock(return_value=False))  # pyright: ignore[reportPrivateUsage]

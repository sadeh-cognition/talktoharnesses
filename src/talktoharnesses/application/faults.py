"""Test-only async fault-injection checkpoints (Phase 9 WP6).

Production composition leaves ``fault_callback`` as ``None``. Checkpoints never
read environment variables, Django settings, or HTTP state.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from enum import StrEnum


class FaultPoint(StrEnum):
    AFTER_CLAIM_COMMIT = "after_claim_commit"
    AFTER_DELIVERY_STARTED = "after_delivery_started"
    AFTER_ADAPTER_RECEIPT = "after_adapter_receipt"
    AFTER_NATIVE_ACK = "after_native_ack"
    AFTER_DELIVERED = "after_delivered"
    AFTER_EVENT_COMMIT = "after_event_commit"
    AFTER_PUBLICATION = "after_publication"
    AFTER_NATIVE_RESUME_COMMIT = "after_native_resume_commit"
    AFTER_FALLBACK_SEED = "after_fallback_seed"
    AFTER_SESSION_ROTATION_COMMIT = "after_session_rotation_commit"


FaultCallback = Callable[[FaultPoint], Awaitable[None]] | None


async def checkpoint(cb: FaultCallback, point: FaultPoint) -> None:
    """Invoke ``cb`` at ``point``; no-op when the callback is unset."""
    if cb is None:
        return
    await cb(point)

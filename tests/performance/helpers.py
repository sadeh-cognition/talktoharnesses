"""Timing helpers for fixed release performance budgets."""

from __future__ import annotations

import math
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")


def percentile_ns(samples: list[int], pct: float) -> int:
    if not samples:
        raise ValueError("no samples")
    ordered = sorted(samples)
    index = min(len(ordered) - 1, max(0, math.ceil(pct / 100.0 * len(ordered)) - 1))
    return ordered[index]


async def measure_p95_ns(
    operation: Callable[[], Awaitable[T]],
    *,
    warmups: int = 5,
    samples: int = 30,
) -> tuple[int, list[T]]:
    """Run warmups then samples; return p95 nanoseconds and sample results."""
    results: list[T] = []
    for _ in range(warmups):
        await operation()
    timings: list[int] = []
    for _ in range(samples):
        started = time.perf_counter_ns()
        results.append(await operation())
        timings.append(time.perf_counter_ns() - started)
    return percentile_ns(timings, 95), results

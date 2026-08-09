"""Retention cutoff: months_before month-end clamping and leap years."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from talktoharnesses.application.retention import months_before, six_months_before


@pytest.mark.parametrize(
    ("now", "months", "expected"),
    [
        # Ordinary same-day shift (default six months).
        (
            datetime(2026, 8, 9, 1, 17, tzinfo=UTC),
            6,
            datetime(2026, 2, 9, 1, 17, tzinfo=UTC),
        ),
        # Month-end clamp: Aug 31 -> Feb 28 (2026 is not a leap year).
        (datetime(2026, 8, 31, tzinfo=UTC), 6, datetime(2026, 2, 28, tzinfo=UTC)),
        # Leap-year month-end clamp: Aug 31, 2024 -> Feb 29, 2024.
        (datetime(2024, 8, 31, tzinfo=UTC), 6, datetime(2024, 2, 29, tzinfo=UTC)),
        # Year rollover.
        (datetime(2026, 1, 15, tzinfo=UTC), 6, datetime(2025, 7, 15, tzinfo=UTC)),
        (datetime(2026, 3, 31, tzinfo=UTC), 6, datetime(2025, 9, 30, tzinfo=UTC)),
        # One-month policy.
        (datetime(2026, 8, 9, tzinfo=UTC), 1, datetime(2026, 7, 9, tzinfo=UTC)),
        # 120-month policy.
        (datetime(2026, 8, 9, tzinfo=UTC), 120, datetime(2016, 8, 9, tzinfo=UTC)),
    ],
)
def test_months_before(now: datetime, months: int, expected: datetime) -> None:
    assert months_before(now, months) == expected


def test_six_months_before_wraps_months_before() -> None:
    now = datetime(2026, 8, 9, 1, 17, tzinfo=UTC)
    assert six_months_before(now) == months_before(now, 6)


def test_months_before_preserves_time_of_day() -> None:
    now = datetime(2026, 8, 9, 13, 45, 30, tzinfo=UTC)
    cutoff = months_before(now, 6)
    assert (cutoff.hour, cutoff.minute, cutoff.second) == (13, 45, 30)


def test_months_before_normalizes_to_utc() -> None:
    tz = UTC
    naive_offset_now = datetime(2026, 8, 9, 5, tzinfo=tz) - timedelta(hours=0)
    cutoff = months_before(naive_offset_now, 6)
    assert cutoff.tzinfo is UTC


def test_months_before_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        months_before(datetime(2026, 8, 9), 6)

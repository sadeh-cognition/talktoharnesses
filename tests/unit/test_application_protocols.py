"""Smoke imports for application protocols (no implementations in Phase 1)."""

from __future__ import annotations

from talktoharnesses.application import CommittedEventPublisher, Persistence


def test_protocols_are_importable() -> None:
    assert Persistence is not None
    assert CommittedEventPublisher is not None
    assert "publish" in CommittedEventPublisher.__dict__ or hasattr(
        CommittedEventPublisher, "publish"
    )

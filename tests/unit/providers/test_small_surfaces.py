"""Small public helper surfaces that were previously uncovered."""

from __future__ import annotations

from talktoharnesses.providers.acp.schemas.grok_ext import GrokControlNotification
from talktoharnesses.providers.grok import render_supported as grok_render


def test_grok_control_notification_round_trip() -> None:
    note = GrokControlNotification.model_validate(
        {"method": "grok/control", "params": {"ok": True}}
    )
    assert note.method == "grok/control"
    assert note.params["ok"] is True


def test_grok_render_supported_reexports_main() -> None:
    assert callable(grok_render.main)

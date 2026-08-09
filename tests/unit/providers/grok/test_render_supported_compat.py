"""Cover grok render_supported shim and grok_ext schema modules."""

from __future__ import annotations

from talktoharnesses.providers.acp.schemas.grok_ext import GrokControlNotification
from talktoharnesses.providers.grok import render_supported as grok_render


def test_grok_control_notification_schema() -> None:
    note = GrokControlNotification(method="session/info", params={"a": 1})
    assert note.method == "session/info"
    assert note.params == {"a": 1}


def test_grok_render_supported_reexports_main() -> None:
    assert callable(grok_render.main)

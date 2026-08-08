"""Backward-compatible CLI entry; prefer ``providers.render_supported``."""

from __future__ import annotations

from talktoharnesses.providers.render_supported import main

if __name__ == "__main__":
    raise SystemExit(main())

"""Cursor process argument construction (no shell)."""

from __future__ import annotations


def build_cursor_argv() -> tuple[str, ...]:
    """Build argv after the resolved executable.

    Model family, parameters, and workflow mode are selected via ACP
    ``session/set_config_option`` after initialize — not via CLI flags.
    """
    return ("acp",)

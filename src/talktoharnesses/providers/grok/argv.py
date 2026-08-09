"""Grok process argument construction (no shell)."""

from __future__ import annotations


def build_grok_argv(*, model: str | None = None) -> tuple[str, ...]:
    """Build argv after the resolved executable.

    Layout: ``--permission-mode default agent --no-leader [--model <id>] stdio``.
    Never includes ``--always-approve`` / ``--yolo``. Global ``--permission-mode``
    overrides user config that would auto-approve and bypass the broker.
    """
    args: list[str] = ["--permission-mode", "default", "agent", "--no-leader"]
    if model:
        args.extend(["--model", model])
    args.append("stdio")
    return tuple(args)

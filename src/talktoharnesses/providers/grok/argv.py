"""Grok process argument construction (no shell)."""

from __future__ import annotations


def build_grok_argv(*, model: str | None = None) -> tuple[str, ...]:
    """Build argv after the resolved executable.

    Layout: ``agent --no-leader [--model <id>] stdio``.
    Never includes ``--always-approve`` / ``--yolo``.
    """
    args: list[str] = ["agent", "--no-leader"]
    if model:
        args.extend(["--model", model])
    args.append("stdio")
    return tuple(args)

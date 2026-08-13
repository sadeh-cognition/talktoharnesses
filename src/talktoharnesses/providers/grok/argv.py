"""Grok process argument construction (no shell)."""

from __future__ import annotations


def build_grok_argv(*, model: str | None = None, yolo: bool = False) -> tuple[str, ...]:
    """Build argv after the resolved executable.

    Default layout: ``--permission-mode default agent --no-leader [--model <id>] stdio``.
    Global ``--permission-mode default`` overrides user config that would
    auto-approve and bypass the broker. When ``yolo`` is true, launch with
    ``--always-approve`` instead.
    """
    args: list[str] = (
        ["--always-approve", "agent", "--no-leader"]
        if yolo
        else ["--permission-mode", "default", "agent", "--no-leader"]
    )
    if model:
        args.extend(["--model", model])
    args.append("stdio")
    return tuple(args)

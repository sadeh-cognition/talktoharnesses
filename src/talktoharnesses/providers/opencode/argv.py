"""OpenCode serve launch arguments."""

from __future__ import annotations


def build_opencode_argv(*, hostname: str = "127.0.0.1", port: int) -> tuple[str, ...]:
    return ("serve", "--hostname", hostname, "--port", str(port))

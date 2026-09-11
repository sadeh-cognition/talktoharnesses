"""The tth-<kind> split projects, shared by every script that iterates over them."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Directory suffixes: the split for kind ``k`` lives at ``ROOT / f"tth-{k}"``.
KINDS: tuple[str, ...] = ("grok", "cursor", "codex", "claude", "opencode", "prime-agent", "muse")


def package_name(kind: str) -> str:
    """Python package inside the split, e.g. ``tth_prime_agent``."""
    return "tth_" + kind.replace("-", "_")


def split_root(kind: str) -> Path:
    return ROOT / f"tth-{kind}"

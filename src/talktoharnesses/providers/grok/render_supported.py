"""CLI entry to regenerate SUPPORTED_HARNESSES.md from packaged data."""

from __future__ import annotations

import argparse
from pathlib import Path

from talktoharnesses.providers.grok.compatibility import (
    load_grok_compatibility,
    render_supported_harnesses_markdown,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render SUPPORTED_HARNESSES.md")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if the on-disk file differs from regenerated content.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output path (default: repo-root SUPPORTED_HARNESSES.md).",
    )
    args = parser.parse_args(argv)

    content = render_supported_harnesses_markdown(load_grok_compatibility())
    if args.output is not None:
        out = args.output
    else:
        # providers/grok/render_supported.py -> repo root
        out = Path(__file__).resolve().parents[4] / "SUPPORTED_HARNESSES.md"

    if args.check:
        if not out.is_file():
            print(f"missing {out}")
            return 1
        existing = out.read_text(encoding="utf-8")
        if existing != content:
            print(f"{out} is out of date; run without --check to regenerate")
            return 1
        print(f"{out} is up to date")
        return 0

    out.write_text(content, encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

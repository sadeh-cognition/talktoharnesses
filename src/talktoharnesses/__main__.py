"""Demo CLI: ``python -m talktoharnesses --harness codex "list files"``."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from talktoharnesses import harness
from talktoharnesses.registry import KNOWN_HARNESS_NAMES, ensure_drivers_loaded, registered_names


def build_parser() -> argparse.ArgumentParser:
    ensure_drivers_loaded()
    available = registered_names() or list(KNOWN_HARNESS_NAMES)
    parser = argparse.ArgumentParser(
        prog="talktoharnesses",
        description="Drive a coding-agent harness through the unified async API.",
    )
    parser.add_argument(
        "--harness",
        "-H",
        required=True,
        choices=sorted(set(available) | set(KNOWN_HARNESS_NAMES)),
        help="Harness name",
    )
    parser.add_argument(
        "--cwd",
        default=".",
        help="Working directory for the agent (default: .)",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Optional model override",
    )
    parser.add_argument(
        "prompt",
        nargs="+",
        help="Prompt to send",
    )
    parser.add_argument(
        "--accept-all",
        action="store_true",
        help="Automatically accept approval requests",
    )
    return parser


async def _run(args: argparse.Namespace) -> int:
    prompt = " ".join(args.prompt)
    cwd = Path(args.cwd).resolve()
    kwargs: dict[str, object] = {}
    if args.model:
        kwargs["model"] = args.model

    async with harness(args.harness, cwd=cwd, **kwargs) as h:
        await h.start_session()
        async for ev in h.send_turn(prompt):
            if ev.type == "content.delta":
                text = getattr(ev, "text", "")
                sys.stdout.write(text)
                sys.stdout.flush()
            elif ev.type == "request.opened":
                rid = getattr(ev, "request_id", None)
                title = getattr(ev, "title", None) or getattr(ev, "tool_name", None)
                print(f"\n[approval] {title} (id={rid})", file=sys.stderr)
                if args.accept_all and rid:
                    await h.respond(rid, "accept")
                    print("[approval] accepted", file=sys.stderr)
                else:
                    print(
                        "[approval] respond via API or re-run with --accept-all",
                        file=sys.stderr,
                    )
            elif ev.type == "runtime.error":
                print(f"\n[error] {getattr(ev, 'message', ev)}", file=sys.stderr)
            elif ev.type == "turn.completed":
                print(file=sys.stdout)
            elif ev.type == "turn.aborted":
                print(
                    f"\n[aborted] {getattr(ev, 'reason', '')}",
                    file=sys.stderr,
                )
                return 1
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        raise SystemExit(asyncio.run(_run(args)))
    except KeyboardInterrupt:
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()

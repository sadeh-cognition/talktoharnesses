"""Deterministic helper process for runtime supervision tests.

Modes:
  stdout_bytes HEX...   — write raw bytes to stdout
  malformed_stdout      — non-UTF8 / protocol-garbage on stdout
  split_stdout          — write stdout in small chunks with delays
  large_stderr N        — write N bytes of stderr (repeating pattern)
  secret_stderr         — write a secret split across flushes
  silence SECONDS       — produce no stdout for SECONDS then exit 0
  hang_start            — ignore everything and sleep forever
  ignore_interrupt      — ignore SIGINT; exit only on SIGTERM/SIGKILL
  spawn_descendant      — spawn a long-lived child then wait
  exit_code N           — exit with code N immediately
  echo_line             — read stdin lines and echo to stdout
"""

from __future__ import annotations

import os
import signal
import sys
import time


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: child_modes.py <mode> [args]", file=sys.stderr)
        return 2
    mode, *rest = argv

    if mode == "stdout_bytes":
        data = bytes.fromhex(rest[0]) if rest else b"\x00\xff"
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
        return 0

    if mode == "malformed_stdout":
        sys.stdout.buffer.write(b"\xff\xfe not json \x00\x01")
        sys.stdout.buffer.flush()
        # Also write something on stderr that must not appear on stdout.
        sys.stderr.write("stderr-only-marker\n")
        sys.stderr.flush()
        return 0

    if mode == "split_stdout":
        for part in (b"hel", b"lo ", b"wor", b"ld\n"):
            sys.stdout.buffer.write(part)
            sys.stdout.buffer.flush()
            time.sleep(0.05)
        return 0

    if mode == "large_stderr":
        n = int(rest[0]) if rest else 1024
        chunk = b"x" * 4096
        written = 0
        while written < n:
            piece = chunk[: min(len(chunk), n - written)]
            sys.stderr.buffer.write(piece)
            sys.stderr.buffer.flush()
            written += len(piece)
        return 0

    if mode == "secret_stderr":
        # Split secret across two writes so streaming redaction must carry state.
        sys.stderr.write("prefix-SECR")
        sys.stderr.flush()
        time.sleep(0.05)
        sys.stderr.write("ET-suffix\n")
        sys.stderr.flush()
        return 0

    if mode == "silence":
        seconds = float(rest[0]) if rest else 5.0
        time.sleep(seconds)
        sys.stdout.write("done\n")
        sys.stdout.flush()
        return 0

    if mode == "hang_start":
        while True:
            time.sleep(3600)

    if mode == "ignore_interrupt":
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        # Also ignore keyboard interrupt on Windows via SIGBREAK if present.
        sigbreak = getattr(signal, "SIGBREAK", None)
        if isinstance(sigbreak, int):
            signal.signal(sigbreak, signal.SIG_IGN)
        while True:
            time.sleep(0.2)

    if mode == "spawn_descendant":
        if not hasattr(os, "fork"):
            return 1
        pid = os.fork()
        if pid == 0:
            # Grandchild: ignore SIGHUP, sleep long.
            if hasattr(signal, "SIGHUP"):
                signal.signal(signal.SIGHUP, signal.SIG_IGN)
            time.sleep(3600)
            os._exit(0)
        # Parent waits forever (until killed with the group).
        while True:
            time.sleep(1)

    if mode == "exit_code":
        return int(rest[0]) if rest else 1

    if mode == "echo_line":
        for line in sys.stdin:
            sys.stdout.write(line)
            sys.stdout.flush()
        return 0

    print(f"unknown mode: {mode}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

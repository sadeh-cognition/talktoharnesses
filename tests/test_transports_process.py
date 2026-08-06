"""ManagedProcess tests."""

from __future__ import annotations

import asyncio
import sys

import pytest

from talktoharnesses.errors import ProcessError
from talktoharnesses.transports.process import spawn_process


async def test_spawn_and_wait_echo() -> None:
    proc = await spawn_process(
        [sys.executable, "-c", "import sys; sys.exit(0)"],
    )
    code = await proc.wait()
    assert code == 0
    assert not proc.is_running()


async def test_spawn_missing_binary() -> None:
    with pytest.raises(ProcessError, match="not found"):
        await spawn_process(["definitely-not-a-real-binary-xyzzy"])


async def test_terminate_long_running() -> None:
    proc = await spawn_process(
        [sys.executable, "-c", "import time; time.sleep(60)"],
    )
    assert proc.is_running()
    code = await proc.terminate(timeout=2.0)
    assert code is not None
    assert not proc.is_running()


async def test_aclose_closes_stdin() -> None:
    proc = await spawn_process(
        [sys.executable, "-c", "import sys; sys.stdin.read(); sys.exit(0)"],
    )
    code = await proc.aclose(timeout=2.0)
    assert code == 0


# ---------------------------------------------------------------------------
# Regression: stderr must be drained. An unread pipe fills and blocks the
# child once past asyncio's buffer high-water mark, wedging the harness.
# ---------------------------------------------------------------------------

_NOISY_CHILD = (
    "import sys\n"
    "sys.stderr.write('E' * 5_000_000)\n"
    "sys.stderr.flush()\n"
    "sys.stdout.write('done\\n')\n"
    "sys.stdout.flush()\n"
)


async def test_heavy_stderr_does_not_block_the_child() -> None:
    proc = await spawn_process([sys.executable, "-c", _NOISY_CHILD])
    assert proc.stdout is not None
    try:
        line = await asyncio.wait_for(proc.stdout.readline(), timeout=10.0)
        assert line == b"done\n"
    finally:
        await proc.aclose(timeout=5.0)


async def test_stderr_tail_is_captured_and_bounded() -> None:
    child = (
        "import sys\n"
        "sys.stderr.write('x' * 300000)\n"
        "sys.stderr.write('AUTH FAILED: not logged in\\n')\n"
    )
    proc = await spawn_process([sys.executable, "-c", child])
    await proc.aclose(timeout=5.0)

    tail = proc.stderr_tail()
    # The useful part — the last thing the CLI said — survives.
    assert "AUTH FAILED: not logged in" in tail
    # ...but the buffer is bounded, so the 300KB of noise is not retained.
    assert len(proc._stderr_buf) <= proc.stderr_tail_bytes
    assert len(tail) <= 2001  # max_chars + the elision marker


async def test_stderr_tail_empty_for_quiet_child() -> None:
    proc = await spawn_process([sys.executable, "-c", "pass"])
    await proc.aclose(timeout=5.0)
    assert proc.stderr_tail() == ""

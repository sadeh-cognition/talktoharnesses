"""Raw MSP wire capture for post-mortem debugging.

Every JSON-RPC frame the split reads from the Muse host's stdout and every
frame it writes to stdin is appended, unredacted, to one JSONL file per split
session. The capture is opt-in: it runs only when ``TTH_MUSE_WIRE_LOG_DIR``
names a directory (``/data/wire`` is the ``tth-muse-data`` volume in the
sandbox image). Opening a new capture prunes the oldest files so at most
``TTH_MUSE_WIRE_LOG_KEEP`` (default 20) session files remain. Notes about the
reader's own lifecycle (start, exit reason, silence warnings, dropped
notifications) go into the same file so a stall can be placed relative to the
last frame that crossed the pipe.

The file is the ground truth for "did the host send it?" questions: the
supervised host journals its work to its own session store, the proxy only
sees what the normalizer forwards, and this file sits between the two.

Records are one JSON object per line::

    {"ts": "<UTC ISO-8601>", "mono": <monotonic seconds>, "session": "<split
     session id>", "dir": "in" | "out" | "note", "frame": {...} | "text": "..."}

The capture holds raw prompts, tool output and any secrets the host echoes;
it never leaves the container unless copied out deliberately.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)

_DIR_ENV_VAR = "TTH_MUSE_WIRE_LOG_DIR"
_KEEP_ENV_VAR = "TTH_MUSE_WIRE_LOG_KEEP"
DEFAULT_RETENTION = 20


def wire_log_dir() -> Path | None:
    """Directory for wire captures, or ``None`` when capture is disabled."""
    raw = os.environ.get(_DIR_ENV_VAR, "")
    if not raw.strip():
        return None
    return Path(raw).expanduser()


def wire_log_retention() -> int:
    """How many session files to keep; unparsable values fall back to the default."""
    raw = os.environ.get(_KEEP_ENV_VAR, "")
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_RETENTION


def _prune(directory: Path, keep: int) -> None:
    """Delete the oldest ``*.jsonl`` captures so at most ``keep`` remain."""
    try:
        files = sorted(directory.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    except OSError:
        return
    for stale in files[: max(0, len(files) - keep)]:
        with contextlib.suppress(OSError):
            stale.unlink()


class WireLog:
    """Append-only JSONL capture for one split session.

    Writes are small ``os.write`` calls on an ``O_APPEND`` descriptor, so they
    are cheap enough to run inline on the event loop and never reorder. A
    capture that cannot be opened or written disables itself with one warning
    rather than interfering with the session.
    """

    def __init__(self, session_id: UUID, directory: Path) -> None:
        self.session_id = session_id
        self.path = directory / f"{session_id}.jsonl"
        self._fd: int | None = None
        try:
            directory.mkdir(parents=True, exist_ok=True)
            # Keep one slot for this session's file.
            _prune(directory, max(0, wire_log_retention() - 1))
            self._fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        except OSError:
            logger.warning("wire capture disabled for %s: cannot open %s", session_id, self.path)
            self._fd = None

    @classmethod
    def open(cls, session_id: UUID) -> WireLog | None:
        """Open the capture for a session, or ``None`` when disabled."""
        directory = wire_log_dir()
        if directory is None:
            return None
        log = cls(session_id, directory)
        if log._fd is None:
            return None
        logger.info("wire capture for session %s at %s", session_id, log.path)
        return log

    @property
    def enabled(self) -> bool:
        return self._fd is not None

    def inbound(self, frame: dict[str, Any]) -> None:
        self._record({"dir": "in", "frame": frame})

    def outbound(self, frame: dict[str, Any]) -> None:
        self._record({"dir": "out", "frame": frame})

    def note(self, text: str, **fields: Any) -> None:
        self._record({"dir": "note", "text": text, **fields})

    def close(self, reason: str) -> None:
        if self._fd is None:
            return
        self.note("capture closed", reason=reason)
        with contextlib.suppress(OSError):
            os.close(self._fd)
        self._fd = None

    def _record(self, body: dict[str, Any]) -> None:
        if self._fd is None:
            return
        record = {
            "ts": datetime.now(UTC).isoformat(timespec="microseconds"),
            "mono": time.monotonic(),
            "session": str(self.session_id),
            **body,
        }
        try:
            line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
            os.write(self._fd, line.encode("utf-8"))
        except (OSError, TypeError, ValueError):
            logger.warning(
                "wire capture disabled for %s: write failed", self.session_id, exc_info=True
            )
            with contextlib.suppress(OSError):
                os.close(self._fd)
            self._fd = None

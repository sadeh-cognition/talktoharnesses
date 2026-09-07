"""Wire capture writes one JSONL record per frame and disables itself cleanly."""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from tth_muse.harness.wire_log import WireLog, wire_log_dir, wire_log_retention


def test_wire_log_records_frames_and_notes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TTH_MUSE_WIRE_LOG_DIR", str(tmp_path / "wire"))
    session_id = uuid4()
    log = WireLog.open(session_id)
    assert log is not None and log.enabled

    log.inbound({"jsonrpc": "2.0", "method": "turn/started", "params": {"turnId": "t1"}})
    log.outbound({"jsonrpc": "2.0", "id": "1", "method": "turn/interrupt"})
    log.note("reader exiting", reason="closed", frames_in=1)
    log.close("test")
    assert not log.enabled

    lines = [json.loads(line) for line in log.path.read_text().splitlines()]
    assert [row["dir"] for row in lines] == ["in", "out", "note", "note"]
    assert all(row["session"] == str(session_id) for row in lines)
    assert lines[0]["frame"]["params"]["turnId"] == "t1"
    assert lines[2]["text"] == "reader exiting" and lines[2]["frames_in"] == 1
    assert lines[3]["reason"] == "test"
    assert log.path.parent == tmp_path / "wire"


@pytest.mark.parametrize("value", [None, "", "   "])
def test_wire_log_is_opt_in(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    if value is None:
        monkeypatch.delenv("TTH_MUSE_WIRE_LOG_DIR", raising=False)
    else:
        monkeypatch.setenv("TTH_MUSE_WIRE_LOG_DIR", value)
    assert wire_log_dir() is None
    assert WireLog.open(uuid4()) is None


def test_wire_log_prunes_oldest_captures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = tmp_path / "wire"
    directory.mkdir()
    for index in range(4):
        stale = directory / f"old-{index}.jsonl"
        stale.write_text("{}\n")
        os.utime(stale, (index, index))
    monkeypatch.setenv("TTH_MUSE_WIRE_LOG_DIR", str(directory))
    monkeypatch.setenv("TTH_MUSE_WIRE_LOG_KEEP", "3")

    log = WireLog.open(uuid4())
    assert log is not None
    log.close("test")

    remaining = sorted(p.name for p in directory.glob("*.jsonl"))
    assert remaining == sorted(["old-2.jsonl", "old-3.jsonl", log.path.name])


@pytest.mark.parametrize(
    ("raw", "expected"), [("", 20), ("abc", 20), ("0", 0), ("-5", 0), ("7", 7)]
)
def test_wire_log_retention_parsing(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: int
) -> None:
    monkeypatch.setenv("TTH_MUSE_WIRE_LOG_KEEP", raw)
    assert wire_log_retention() == expected


def test_wire_log_unwritable_directory_disables_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("not a directory")
    monkeypatch.setenv("TTH_MUSE_WIRE_LOG_DIR", str(blocker / "wire"))
    assert WireLog.open(uuid4()) is None

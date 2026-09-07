"""The split's LOGGING setting must feed root handlers, or the OTel exporter
installed on the root logger never sees the service's own records."""

from __future__ import annotations

import logging
import logging.config
import time
from datetime import UTC, datetime

import pytest

from tth_cursor import settings
from tth_cursor.shared.logs import UtcFormatter


def test_split_logger_propagates_to_root_handlers() -> None:
    logging.config.dictConfig(settings.LOGGING)
    root = logging.getLogger()
    seen: list[logging.LogRecord] = []
    probe = logging.Handler()
    probe.emit = seen.append  # type: ignore[method-assign]
    root.addHandler(probe)
    try:
        logging.getLogger("tth_cursor.tests.propagation").info("hello from the split")
        logging.getLogger("httpx").info("third-party chatter")
    finally:
        root.removeHandler(probe)

    assert [r.getMessage() for r in seen] == ["hello from the split"]
    assert logging.getLogger("tth_cursor").propagate is True
    assert not logging.getLogger("tth_cursor").handlers


def test_utc_formatter_ignores_process_time_zone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TZ", "America/Chicago")
    time.tzset()
    try:
        formatter = UtcFormatter(fmt="%(asctime)sZ %(message)s", datefmt="%Y-%m-%dT%H:%M:%S")
        record = logging.LogRecord("t", logging.INFO, "", 0, "msg", (), None)
        record.created = 1_700_000_000.0
        expected = datetime.fromtimestamp(1_700_000_000, UTC).strftime("%Y-%m-%dT%H:%M:%S")
        assert formatter.format(record) == f"{expected}Z msg"
    finally:
        monkeypatch.undo()
        time.tzset()

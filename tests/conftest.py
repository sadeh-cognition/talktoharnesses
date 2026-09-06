"""Root test configuration."""

from __future__ import annotations

import pytest

# Modules whose tests wait on real timers, leases, or subprocesses (isolated
# installs, fresh interpreters). ``-n ... --dist=worksteal`` hands each worker
# a contiguous slice of the collection, so spreading these evenly through it
# keeps any one worker from holding a long chain of slow tests at the end of
# the run while the others sit idle.
_SLOW_MODULES = frozenset(
    {
        "tests/test_packaging.py",
        "tests/unit/application/test_command_processor.py",
        "tests/e2e/test_startup_failure_events.py",
        "tests/runtime/test_runtime_manager.py",
        "tests/unit/test_host_telemetry.py",
        "tests/test_docs_ops.py",
    }
)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    slow = [item for item in items if item.nodeid.split("::", 1)[0] in _SLOW_MODULES]
    if not slow or len(slow) == len(items):
        return
    fast = [item for item in items if item.nodeid.split("::", 1)[0] not in _SLOW_MODULES]
    stride = len(fast) / len(slow)
    ordered: list[pytest.Item] = []
    for index, slow_item in enumerate(slow):
        ordered.extend(fast[round(index * stride) : round((index + 1) * stride)])
        ordered.append(slow_item)
    items[:] = ordered

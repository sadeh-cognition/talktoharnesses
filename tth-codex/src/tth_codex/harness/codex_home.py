"""Serialize Codex starts until CODEX_HOME's state exists."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator


class CodexHomeGate:
    """Run Codex process starts alone until one has succeeded.

    Codex processes started together on a fresh CODEX_HOME race to create its
    SQLite state, and the losers exit ("failed to initialize sqlite state
    runtime"). CODEX_HOME is per process, so the split shares one gate across
    every adapter: a start that fails leaves the gate closed for the next one.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._ready = False

    @contextlib.asynccontextmanager
    async def start(self) -> AsyncGenerator[None]:
        if self._ready:
            yield
            return
        async with self._lock:
            yield
            self._ready = True

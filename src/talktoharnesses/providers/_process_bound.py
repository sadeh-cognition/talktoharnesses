"""Internal process-bound adapter hooks (not part of HarnessAdapter)."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from talktoharnesses.domain.models import HarnessConfiguration
from talktoharnesses.runtime.handle import ProcessHandle


@runtime_checkable
class ProcessBoundAdapter(Protocol):
    """Duck-typed hooks for adapters that speak over a supervised process."""

    def bind_process(self, process: ProcessHandle) -> None: ...

    def build_argv(self, config: HarnessConfiguration) -> tuple[str, ...]: ...

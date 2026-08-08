"""Internal marker for adapters whose subprocess is owned by an SDK."""

from __future__ import annotations

from typing import ClassVar, Literal, Protocol, runtime_checkable


@runtime_checkable
class SdkManagedAdapter(Protocol):
    """Duck-typed marker: RuntimeManager skips ProcessSupervisor spawn."""

    sdk_managed: ClassVar[Literal[True]]

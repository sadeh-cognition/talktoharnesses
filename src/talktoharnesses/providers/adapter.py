"""Fixed harness adapter contract (re-exported from tth-types)."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from tth_types.adapter import HarnessAdapter as HarnessAdapter
from tth_types.adapter import HarnessInteractionRequest as HarnessInteractionRequest
from tth_types.adapter import HarnessSession as HarnessSession
from tth_types.adapter import ResumeSessionRequest as ResumeSessionRequest
from tth_types.adapter import StartSessionRequest as StartSessionRequest
from tth_types.adapter import SteerRequest as SteerRequest
from tth_types.adapter import TurnRequest as TurnRequest
from tth_types.harness import HarnessConfiguration

from talktoharnesses.domain.models import SplitStreamCursor


@runtime_checkable
class SplitStreamAdapter(Protocol):
    """An adapter streaming a split session that can outlive the proxy.

    The split dedupes a replayed native history against the seen sets the
    proxy imports and exports. The proxy commits ``split_cursor`` with its
    events and, after a restart, ``reattach``-es to the surviving session to
    replay the frames after it.
    """

    def import_seen(self, native_ids: frozenset[str], stream_offsets: frozenset[str]) -> None: ...

    def export_seen(self) -> tuple[frozenset[str], frozenset[str]]: ...

    def split_cursor(self) -> SplitStreamCursor | None: ...

    async def reattach(
        self,
        session: HarnessSession,
        *,
        configuration: HarnessConfiguration,
        cursor: SplitStreamCursor,
    ) -> None: ...

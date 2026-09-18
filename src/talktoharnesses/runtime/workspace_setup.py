"""Run a repo-declared ``.tth/setup.sh`` before a session opens, recording its events.

Every session-opening path in :mod:`talktoharnesses.runtime.manager` (client
start or resume, recovery resume, candidate runtime) calls
:func:`prepare_workspace`. Paths that write lifecycle rows hand it a
:class:`WorkspaceSetupRecorder`; candidates pass none and record nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TYPE_CHECKING, Protocol, cast
from uuid import UUID

from talktoharnesses.application.observability import get_observability
from talktoharnesses.application.persistence import Persistence
from talktoharnesses.domain import DomainError, ErrorCode, append_events
from talktoharnesses.domain.events import (
    EventPayload,
    WorkspaceSetupCompletedPayload,
    WorkspaceSetupStartedPayload,
)
from talktoharnesses.domain.models import HarnessConfiguration
from talktoharnesses.domain.transitions import ConversationState
from talktoharnesses.providers.adapter import HarnessAdapter
from talktoharnesses.remote.sandbox_workspace import WorkspaceSetupFailed

if TYPE_CHECKING:
    from talktoharnesses.remote.sandbox_workspace import (
        WorkspaceSetupOutcome,
        WorkspaceSetupStarted,
    )

logger = logging.getLogger(__name__)


class WorkspacePreparer(Protocol):
    """Duck-typed adapter hook: run a repo-declared setup script before a session."""

    async def prepare_workspace(
        self,
        configuration: HarnessConfiguration,
        *,
        on_started: Callable[[WorkspaceSetupStarted], None] | None = None,
    ) -> WorkspaceSetupOutcome | None: ...


def workspace_preparer(adapter: HarnessAdapter) -> WorkspacePreparer | None:
    if callable(getattr(adapter, "prepare_workspace", None)):
        return cast(WorkspacePreparer, adapter)
    return None


class WorkspaceSetupRecorder:
    """Owns every persistence write one workspace setup run produces.

    The events are committed without a process row: setup says nothing about
    the process, so a stray commit can never change its status. The
    preparer's ``on_started`` may arrive from a worker thread the manager
    cannot cancel, so after :meth:`finish` nothing new is committed and a
    started commit still in flight is cancelled when the run was abandoned.
    Conflicting commits are retried so a concurrent client write never
    replaces the setup outcome with a persistence error.
    """

    def __init__(
        self,
        persistence: Persistence,
        clock: Callable[[], datetime],
        *,
        conversation_id: UUID,
        binding_id: UUID,
        worker_id: str | None,
        fence: int | None,
        refresh: Callable[[], Awaitable[ConversationState]],
    ) -> None:
        self._persistence = persistence
        self._clock = clock
        self._conversation_id = conversation_id
        self._binding_id = binding_id
        self._worker_id = worker_id
        self._fence = fence
        # Reads the conversation the way the opening path does (owner-scoped
        # for client starts, worker-scoped for recovery).
        self._refresh = refresh
        self._started: asyncio.Task[None] | None = None
        self._finished = False

    def on_started(self, event: WorkspaceSetupStarted) -> None:
        if self._finished or self._started is not None:
            return  # late (the run was abandoned) or duplicate progress event
        self._started = asyncio.create_task(
            self._commit(
                WorkspaceSetupStartedPayload(
                    binding_id=self._binding_id,
                    working_directory=event.working_directory,
                    setup_file=event.setup_file,
                    stamp=event.stamp,
                )
            ),
            name=f"workspace-setup-started-{self._conversation_id}",
        )

    async def finish(self, *, cancel: bool = False) -> None:
        """End the run: let the progress commit land, or cancel it when abandoned.

        A lost progress event is logged, never raised: it must not mask the
        setup outcome itself.
        """
        self._finished = True
        task = self._started
        if task is None:
            return
        if cancel:
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            if not cancel:
                raise
        except Exception:
            logger.warning(
                "workspace_setup_started commit failed conversation=%s",
                self._conversation_id,
                exc_info=True,
            )

    async def record_failure(self, exc: WorkspaceSetupFailed) -> None:
        """Record a failed run; persistence trouble is logged, the outcome wins."""
        try:
            await self._commit(
                WorkspaceSetupCompletedPayload(
                    binding_id=self._binding_id,
                    working_directory=exc.working_directory,
                    status=exc.completed_status,
                    exit_code=exc.exit_code,
                    output_tail=exc.output_tail,
                )
            )
        except Exception:
            logger.warning(
                "workspace_setup_completed commit failed conversation=%s",
                self._conversation_id,
                exc_info=True,
            )

    async def record_success(self, outcome: WorkspaceSetupOutcome) -> None:
        await self._commit(
            WorkspaceSetupCompletedPayload(
                binding_id=self._binding_id,
                working_directory=outcome.working_directory,
                status="succeeded",
                exit_code=outcome.exit_code,
                duration_ms=outcome.duration_ms,
                output_tail=outcome.output_tail,
            )
        )

    async def _commit(self, payload: EventPayload) -> None:
        while True:
            fresh = await self._refresh()
            new_state, events = append_events(fresh, self._clock(), [payload])
            try:
                await self._persistence.commit_runtime_lifecycle(
                    self._conversation_id,
                    fresh.conversation.version,
                    new_state,
                    None,
                    None,
                    events,
                    worker_id=self._worker_id,
                    fence=self._fence,
                )
            except DomainError as exc:
                if exc.code is ErrorCode.OPTIMISTIC_CONFLICT:
                    continue
                raise
            get_observability().observe_committed_events(events, state=new_state)
            return


async def prepare_workspace(
    adapter: HarnessAdapter,
    configuration: HarnessConfiguration,
    *,
    recorder: WorkspaceSetupRecorder | None,
) -> None:
    """Run the adapter's setup hook, bracketing a real run with lifecycle events.

    Adapters without the hook, disabled setup, a missing script or a
    matching stamp leave no events behind. A setup failure propagates as
    :class:`WorkspaceSetupFailed` after its completion event is recorded.
    """
    preparer = workspace_preparer(adapter)
    if preparer is None:
        return
    if recorder is None:
        await preparer.prepare_workspace(configuration)
        return
    try:
        outcome = await preparer.prepare_workspace(configuration, on_started=recorder.on_started)
    except WorkspaceSetupFailed as exc:
        await recorder.finish()
        await recorder.record_failure(exc)
        raise
    except BaseException:
        # Cancellation (shutdown) or an unrelated error: the run is abandoned
        # and the session is about to fail; nothing more may be recorded.
        with contextlib.suppress(Exception):
            await recorder.finish(cancel=True)
        raise
    await recorder.finish()
    if outcome is not None and outcome.status == "succeeded":
        await recorder.record_success(outcome)

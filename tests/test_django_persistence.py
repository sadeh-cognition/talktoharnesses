"""SQLite contract checks for the production persistence implementation."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from talktoharnesses.django.persistence import DjangoPersistence
from talktoharnesses.domain import (
    DomainError,
    ErrorCode,
    HarnessCapabilities,
    HarnessConfiguration,
    HarnessKind,
    LaunchSnapshot,
    ProcessRecord,
    ProcessStatus,
    append_events,
    new_conversation_state,
)
from talktoharnesses.domain.events import ProcessExitedPayload
from talktoharnesses.domain.models import ConversationHarnessBinding


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_runtime_lifecycle_round_trip_and_conflict() -> None:
    now = datetime(2026, 8, 8, tzinfo=UTC)
    conversation_id = uuid4()
    configuration = HarnessConfiguration(
        kind=HarnessKind.OPENCODE,
        working_directory="/tmp",
    )
    binding = ConversationHarnessBinding(
        conversation_id=conversation_id,
        kind=HarnessKind.OPENCODE,
        configuration=configuration,
        created_at=now,
    )
    state = new_conversation_state(
        owner_id="owner-1",
        now=now,
        binding=binding,
        conversation_id=conversation_id,
    )
    persistence = DjangoPersistence()
    await persistence.save_snapshot(state)

    capabilities = HarnessCapabilities(kind=HarnessKind.OPENCODE, version="1")
    launch = LaunchSnapshot(
        resolved_executable="/bin/true",
        harness_version="1",
        working_directory="/tmp",
        adapter_version="1",
        capabilities=capabilities,
    )
    process = ProcessRecord(
        conversation_id=conversation_id,
        binding_id=binding.id,
        status=ProcessStatus.RUNNING,
        pid=123,
        started_at=now,
    )
    state = state.model_copy(
        update={"binding": binding.model_copy(update={"launch_snapshot": launch})}
    )
    await persistence.commit_runtime_lifecycle(
        conversation_id,
        0,
        state,
        process,
        launch,
        (),
    )

    final_state, events = append_events(
        state,
        now,
        [ProcessExitedPayload(process_id=process.id, exit_code=0)],
    )
    exited = process.model_copy(
        update={"status": ProcessStatus.EXITED, "exit_code": 0, "exited_at": now}
    )
    await persistence.commit_runtime_lifecycle(
        conversation_id,
        0,
        final_state,
        exited,
        None,
        events,
    )

    loaded = await persistence.get_snapshot(conversation_id, "owner-1")
    assert loaded == final_state
    assert tuple(await persistence.replay(conversation_id, 0, 10, 100_000)) == events
    with pytest.raises(DomainError) as exc_info:
        await persistence.commit_runtime_lifecycle(
            conversation_id,
            0,
            final_state,
            exited,
            None,
            (),
        )
    assert exc_info.value.code is ErrorCode.OPTIMISTIC_CONFLICT

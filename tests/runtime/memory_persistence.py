"""In-memory Persistence for runtime lifecycle and facade contract tests."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from talktoharnesses.application.cursors import clamp_page_limit, decode_cursor, encode_cursor
from talktoharnesses.application.search_documents import build_search_document_from_parts
from talktoharnesses.domain.enums import (
    CommandStatus,
    ErrorCode,
    InteractionStatus,
    TurnStatus,
)
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.domain.events import ConversationEvent
from talktoharnesses.domain.models import (
    ActivityProjection,
    BackgroundActivity,
    CanonicalToolResult,
    Command,
    CommandProjection,
    ConversationDetail,
    ConversationShell,
    ConversationSnapshot,
    HarnessCapabilities,
    HarnessInstance,
    HarnessProbeProjection,
    HarnessProjection,
    InteractionAnswer,
    InteractionProjection,
    LaunchSnapshot,
    Message,
    MessageProjection,
    Page,
    PendingInteraction,
    Plan,
    PlanProjection,
    ProcessRecord,
    ToolProjection,
    Turn,
    TurnProjection,
)
from talktoharnesses.domain.transitions import ConversationState


def _not_found(resource: str = "conversation") -> DomainError:
    return DomainError(ErrorCode.NOT_FOUND, f"{resource} not found")


class MemoryPersistence:
    """Minimal durable double implementing the Persistence protocol."""

    def __init__(self) -> None:
        self.states: dict[UUID, ConversationState] = {}
        self.processes: dict[UUID, ProcessRecord] = {}  # process_id -> record
        self.launch_history: dict[UUID, list[LaunchSnapshot]] = {}  # conversation
        self.events: dict[UUID, list[ConversationEvent]] = {}
        self.commands: dict[UUID, Command] = {}
        self.accepted_queue: list[UUID] = []
        self.harnesses: dict[UUID, HarnessInstance] = {}
        self.harness_probes: dict[UUID, tuple[HarnessCapabilities, datetime]] = {}
        # Projection stores keyed by conversation then entity id.
        self.turns: dict[UUID, dict[UUID, Turn]] = {}
        self.messages: dict[UUID, dict[UUID, Message]] = {}
        self.tools: dict[UUID, dict[UUID, CanonicalToolResult]] = {}
        self.plans: dict[UUID, dict[UUID, Plan]] = {}
        self.activities: dict[UUID, dict[UUID, BackgroundActivity]] = {}
        self.interactions: dict[UUID, dict[UUID, PendingInteraction]] = {}
        self.interaction_answers: dict[UUID, InteractionAnswer] = {}
        self.search_docs: dict[UUID, str] = {}
        self.turn_order: dict[UUID, list[UUID]] = {}

    def seed(self, state: ConversationState) -> None:
        self.states[state.conversation.id] = state
        self.events.setdefault(state.conversation.id, [])
        self.launch_history.setdefault(state.conversation.id, [])
        self.turns.setdefault(state.conversation.id, {})
        self.messages.setdefault(state.conversation.id, {})
        self.tools.setdefault(state.conversation.id, {})
        self.plans.setdefault(state.conversation.id, {})
        self.activities.setdefault(state.conversation.id, {})
        self.interactions.setdefault(state.conversation.id, {})
        self.turn_order.setdefault(state.conversation.id, [])
        self._refresh_search(state)

    async def get_snapshot(self, conversation_id: UUID, owner_id: str) -> ConversationState:
        try:
            state = self.states[conversation_id]
        except KeyError as exc:
            raise DomainError(
                ErrorCode.INVALID_STATE,
                "conversation not found",
                details={"conversation_id": str(conversation_id)},
            ) from exc
        if state.conversation.owner_id != owner_id:
            raise DomainError(
                ErrorCode.INVALID_STATE,
                "owner mismatch",
                details={"conversation_id": str(conversation_id)},
            )
        if state.conversation.deleted_at is not None:
            raise DomainError(
                ErrorCode.INVALID_STATE,
                "conversation not found",
                details={"conversation_id": str(conversation_id)},
            )
        return state

    async def get_worker_snapshot(self, conversation_id: UUID) -> ConversationState:
        try:
            return self.states[conversation_id]
        except KeyError as exc:
            raise DomainError(
                ErrorCode.INVALID_STATE,
                "conversation not found",
                details={"conversation_id": str(conversation_id)},
            ) from exc

    async def save_snapshot(self, state: ConversationState) -> ConversationState:
        self.states[state.conversation.id] = state
        self.events.setdefault(state.conversation.id, [])
        self.launch_history.setdefault(state.conversation.id, [])
        self.turns.setdefault(state.conversation.id, {})
        self.messages.setdefault(state.conversation.id, {})
        self.tools.setdefault(state.conversation.id, {})
        self.plans.setdefault(state.conversation.id, {})
        self.activities.setdefault(state.conversation.id, {})
        self.interactions.setdefault(state.conversation.id, {})
        self.turn_order.setdefault(state.conversation.id, [])
        self._index_state_projections(state)
        self._refresh_search(state)
        return state

    async def accept_command(self, command: Command) -> Command:
        existing = self.commands.get(command.id)
        if existing is not None:
            return existing
        for stored in self.commands.values():
            if (
                stored.conversation_id == command.conversation_id
                and stored.idempotency_key == command.idempotency_key
            ):
                return stored
        self.commands[command.id] = command
        self.accepted_queue.append(command.id)
        return command

    async def claim_commands(self, worker_id: str, limit: int) -> Sequence[Command]:
        from datetime import UTC, datetime, timedelta

        claimed: list[Command] = []
        still_pending: list[UUID] = []
        now = datetime.now(UTC)
        lease = now + timedelta(seconds=30)
        candidates = list(self.accepted_queue)
        candidates.extend(
            command.id
            for command in self.commands.values()
            if command.status == CommandStatus.CLAIMED
            and command.lease_expires_at is not None
            and command.lease_expires_at < now
            and command.id not in self.accepted_queue
        )
        for command_id in candidates:
            if len(claimed) >= limit:
                still_pending.append(command_id)
                continue
            command = self.commands.get(command_id)
            if command is None or (
                command.status != CommandStatus.ACCEPTED
                and not (
                    command.status == CommandStatus.CLAIMED
                    and command.lease_expires_at is not None
                    and command.lease_expires_at < now
                )
            ):
                continue
            updated = command.model_copy(
                update={
                    "status": CommandStatus.CLAIMED,
                    "worker_id": worker_id,
                    "attempts": command.attempts + 1,
                    "lease_expires_at": lease,
                }
            )
            self.commands[command_id] = updated
            claimed.append(updated)
        self.accepted_queue = still_pending
        return tuple(claimed)

    async def renew_command_lease(
        self,
        command_id: UUID,
        worker_id: str,
        expires_at: datetime,
    ) -> None:
        command = self.commands.get(command_id)
        if command is None or command.worker_id != worker_id:
            raise DomainError(ErrorCode.INVALID_STATE, "command lease not found for worker")
        self.commands[command_id] = command.model_copy(update={"lease_expires_at": expires_at})

    async def update_command(self, command: Command) -> Command:
        self.commands[command.id] = command
        if command.status == CommandStatus.ACCEPTED and command.id not in self.accepted_queue:
            self.accepted_queue.append(command.id)
        return command

    async def commit_event_batch(
        self,
        conversation_id: UUID,
        expected_version: int,
        state: ConversationState,
        events: Sequence[ConversationEvent],
    ) -> Sequence[ConversationEvent]:
        return await self.commit_turn_batch(
            conversation_id,
            expected_version,
            state,
            events,
            (),
        )

    async def commit_turn_batch(
        self,
        conversation_id: UUID,
        expected_version: int,
        state: ConversationState,
        events: Sequence[ConversationEvent],
        commands: Sequence[Command] = (),
    ) -> Sequence[ConversationEvent]:
        committed = await self.commit_runtime_lifecycle(
            conversation_id,
            expected_version,
            state,
            None,
            None,
            events,
        )
        for command in commands:
            self.commands[command.id] = command
            if command.status == CommandStatus.ACCEPTED and command.id not in self.accepted_queue:
                self.accepted_queue.append(command.id)
        return committed

    async def commit_runtime_lifecycle(
        self,
        conversation_id: UUID,
        expected_version: int,
        state: ConversationState,
        process: ProcessRecord | None,
        launch_history_entry: LaunchSnapshot | None,
        events: Sequence[ConversationEvent],
    ) -> Sequence[ConversationEvent]:
        current = self.states.get(conversation_id)
        if current is None:
            raise DomainError(
                ErrorCode.INVALID_STATE,
                "conversation not found",
                details={"conversation_id": str(conversation_id)},
            )
        if current.conversation.version != expected_version:
            raise DomainError(
                ErrorCode.OPTIMISTIC_CONFLICT,
                "optimistic concurrency conflict",
                details={
                    "expected": expected_version,
                    "actual": current.conversation.version,
                },
            )

        if process is not None:
            existing = self.processes.get(process.id)
            if existing is not None and existing.status == process.status:
                pass
            self.processes[process.id] = process
            if process.redacted_stderr_tail or existing is None:
                self.processes[process.id] = process

        if launch_history_entry is not None:
            history = self.launch_history.setdefault(conversation_id, [])
            history.append(launch_history_entry)

        stored_events = self.events.setdefault(conversation_id, [])
        if events:
            for event in events:
                stored_events.append(event)
            self.states[conversation_id] = state
        else:
            self.states[conversation_id] = (
                state.model_copy(
                    update={
                        "conversation": state.conversation.model_copy(
                            update={"version": current.conversation.version}
                        )
                    }
                )
                if state.conversation.version != current.conversation.version
                else state
            )
            if state.conversation.version != expected_version:
                self.states[conversation_id] = current.model_copy(
                    update={
                        "binding": state.binding,
                        "idle_reap_eligible": state.idle_reap_eligible,
                    }
                )
            else:
                self.states[conversation_id] = state

        self._index_state_projections(self.states[conversation_id])
        self._refresh_search(self.states[conversation_id])
        return tuple(events)

    async def replay(
        self,
        conversation_id: UUID,
        after_sequence: int,
        event_count_limit: int,
        byte_limit: int,
    ) -> Sequence[ConversationEvent]:
        items = [e for e in self.events.get(conversation_id, []) if e.sequence > after_sequence]
        return tuple(items[:event_count_limit])

    async def resolve_interaction(
        self,
        interaction_id: UUID,
        answer: InteractionAnswer,
    ) -> InteractionAnswer:
        return self.interaction_answers.setdefault(interaction_id, answer)

    async def delete_expired_turn_aggregates(self, cutoff: datetime) -> int:
        return 0

    async def purge_soft_deleted(self, cutoff: datetime) -> int:
        to_delete = [
            cid
            for cid, state in self.states.items()
            if state.conversation.deleted_at is not None and state.conversation.deleted_at < cutoff
        ]
        for cid in to_delete:
            del self.states[cid]
            self.events.pop(cid, None)
            self.search_docs.pop(cid, None)
        return len(to_delete)

    # ------------------------------------------------------------------
    # Phase 5 facade surface
    # ------------------------------------------------------------------

    async def create_harness(self, harness: HarnessInstance) -> HarnessProjection:
        self.harnesses[harness.id] = harness
        return self._harness_proj(harness)

    async def list_harnesses(
        self,
        owner_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> Page[HarnessProjection]:
        page_size = clamp_page_limit(limit)
        items = [h for h in self.harnesses.values() if h.owner_id == owner_id]
        items.sort(key=lambda h: (h.created_at, h.id), reverse=True)
        if cursor is not None:
            sort, item_id = decode_cursor(cursor)
            sort_dt = datetime.fromisoformat(sort)
            items = [
                h
                for h in items
                if h.created_at < sort_dt or (h.created_at == sort_dt and h.id < item_id)
            ]
        page = items[:page_size]
        next_cursor = None
        if len(items) > page_size and page:
            last = page[-1]
            next_cursor = encode_cursor(sort=last.created_at.isoformat(), id=last.id)
        return Page(items=tuple(self._harness_proj(h) for h in page), next_cursor=next_cursor)

    async def get_harness(self, harness_id: UUID, owner_id: str) -> HarnessProjection:
        harness = self.harnesses.get(harness_id)
        if harness is None or harness.owner_id != owner_id:
            raise _not_found("harness")
        return self._harness_proj(harness)

    async def save_harness_probe(
        self,
        harness_id: UUID,
        owner_id: str,
        capabilities: HarnessCapabilities,
        *,
        probed_at: datetime,
    ) -> HarnessProbeProjection:
        harness = self.harnesses.get(harness_id)
        if harness is None or harness.owner_id != owner_id:
            raise _not_found("harness")
        self.harness_probes[harness_id] = (capabilities, probed_at)
        return HarnessProbeProjection(
            harness_id=harness_id, capabilities=capabilities, probed_at=probed_at
        )

    async def get_harness_probe(
        self,
        harness_id: UUID,
        owner_id: str,
    ) -> HarnessProbeProjection:
        harness = self.harnesses.get(harness_id)
        if harness is None or harness.owner_id != owner_id:
            raise _not_found("harness")
        probe = self.harness_probes.get(harness_id)
        if probe is None:
            raise _not_found("harness probe")
        caps, probed_at = probe
        return HarnessProbeProjection(harness_id=harness_id, capabilities=caps, probed_at=probed_at)

    async def list_conversations(
        self,
        owner_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
        include_archived: bool = True,
    ) -> Page[ConversationShell]:
        page_size = clamp_page_limit(limit)
        shells = [
            self._shell(s)
            for s in self.states.values()
            if s.conversation.owner_id == owner_id and s.conversation.deleted_at is None
        ]
        if not include_archived:
            shells = [s for s in shells if s.archived_at is None]
        shells.sort(key=lambda s: (s.updated_at, s.id), reverse=True)
        if cursor is not None:
            sort, item_id = decode_cursor(cursor)
            sort_dt = datetime.fromisoformat(sort)
            shells = [
                s
                for s in shells
                if s.updated_at < sort_dt or (s.updated_at == sort_dt and s.id < item_id)
            ]
        page = shells[:page_size]
        next_cursor = None
        if len(shells) > page_size and page:
            last = page[-1]
            next_cursor = encode_cursor(sort=last.updated_at.isoformat(), id=last.id)
        return Page(items=tuple(page), next_cursor=next_cursor)

    async def search_conversations(
        self,
        owner_id: str,
        query: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> Page[ConversationShell]:
        page_size = clamp_page_limit(limit)
        needle = " ".join(query.split()).casefold()
        if not needle:
            return Page(items=(), next_cursor=None)
        matches: list[ConversationShell] = []
        for cid, doc in self.search_docs.items():
            state = self.states.get(cid)
            if state is None or state.conversation.owner_id != owner_id:
                continue
            if state.conversation.deleted_at is not None:
                continue
            if needle in doc:
                matches.append(self._shell(state))
        matches.sort(key=lambda s: (s.updated_at, s.id), reverse=True)
        if cursor is not None:
            sort, item_id = decode_cursor(cursor)
            sort_dt = datetime.fromisoformat(sort)
            matches = [
                s
                for s in matches
                if s.updated_at < sort_dt or (s.updated_at == sort_dt and s.id < item_id)
            ]
        page = matches[:page_size]
        next_cursor = None
        if len(matches) > page_size and page:
            last = page[-1]
            next_cursor = encode_cursor(sort=last.updated_at.isoformat(), id=last.id)
        return Page(items=tuple(page), next_cursor=next_cursor)

    async def get_conversation_snapshot(
        self,
        conversation_id: UUID,
        owner_id: str,
    ) -> ConversationSnapshot:
        state = await self._require_owned(conversation_id, owner_id)
        high_water = max(0, state.conversation.next_event_sequence - 1)
        turns = list(self.turns.get(conversation_id, {}).values())
        user_turns = [t for t in turns if t.user_message_id is not None]
        order = {tid: i for i, tid in enumerate(self.turn_order.get(conversation_id, []))}
        user_turns.sort(key=lambda t: order.get(t.id, 0), reverse=True)
        selected = list(reversed(user_turns[:20]))
        turn_projs = tuple(self._turn_proj(t) for t in selected)
        selected_ids = {turn.id for turn in selected}
        selected_messages = sorted(
            (
                message
                for message in self.messages.get(conversation_id, {}).values()
                if message.turn_id in selected_ids
            ),
            key=lambda message: (message.created_at, message.id),
        )
        messages = tuple(
            MessageProjection(
                id=message.id,
                turn_id=message.turn_id,
                role=message.role,
                text=message.text,
                sequence=message.sequence,
                interrupted=message.interrupted,
                created_at=message.created_at,
            )
            for message in selected_messages
        )
        tools = tuple(
            ToolProjection(
                id=tool.id,
                turn_id=tool.turn_id,
                tool_name=tool.tool_name,
                arguments=dict(tool.arguments),
                outcome=tool.outcome,
                exit_status=tool.exit_status,
                paths=tool.paths,
                output_tail=tool.output_tail,
            )
            for tool in self.tools.get(conversation_id, {}).values()
            if tool.turn_id in selected_ids
        )
        plans = tuple(
            PlanProjection(id=plan.id, turn_id=plan.turn_id, items=plan.items)
            for plan in self.plans.get(conversation_id, {}).values()
            if plan.turn_id in selected_ids
        )
        selected_activity = sorted(
            (
                item
                for item in self.activities.get(conversation_id, {}).values()
                if item.parent_turn_id in selected_ids
            ),
            key=lambda item: (item.created_at, item.id),
        )
        activity = tuple(
            ActivityProjection(
                id=item.id,
                conversation_id=item.conversation_id,
                parent_turn_id=item.parent_turn_id,
                parent_activity_id=item.parent_activity_id,
                status=item.status,
                title=item.title,
                summary=item.summary,
                created_at=item.created_at,
                completed_at=item.completed_at,
            )
            for item in selected_activity
        )

        pending = [
            self._interaction_proj(i)
            for i in self.interactions.get(conversation_id, {}).values()
            if i.status in {InteractionStatus.PENDING, InteractionStatus.DRAFT}
        ]
        pending.sort(key=lambda i: (i.created_at, i.id))

        active_command = None
        cmd_id = None
        if state.active_turn is not None:
            cmd_id = state.active_turn.command_id
        elif state.queued_turn is not None:
            cmd_id = state.queued_turn.command_id
        if cmd_id is not None:
            cmd = state.commands.get(cmd_id) or self.commands.get(cmd_id)
            if cmd is not None:
                active_command = CommandProjection(
                    id=cmd.id,
                    kind=cmd.kind,
                    status=cmd.status,
                    target_turn_id=cmd.target_turn_id,
                    idempotency_key=cmd.idempotency_key,
                    created_at=cmd.created_at,
                )

        detail = ConversationDetail(
            conversation=state.conversation,
            harness_kind=state.binding.kind if state.binding else None,
            model=state.binding.configuration.model if state.binding else None,
            mode=state.binding.configuration.mode if state.binding else None,
            turns=turn_projs,
            messages=messages,
            tools=tools,
            plans=plans,
            activity=activity,
            pending_interactions=tuple(pending),
            active_command=active_command,
        )
        return ConversationSnapshot(sequence=high_water, detail=detail)

    async def get_high_water_sequence(
        self,
        conversation_id: UUID,
        owner_id: str,
        *,
        include_deleted: bool = False,
    ) -> int:
        state = self.states.get(conversation_id)
        if (
            state is None
            or state.conversation.owner_id != owner_id
            or (state.conversation.deleted_at is not None and not include_deleted)
        ):
            raise _not_found("conversation")
        return max(0, state.conversation.next_event_sequence - 1)

    async def page_turns(
        self,
        conversation_id: UUID,
        owner_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> Page[TurnProjection]:
        await self._require_owned(conversation_id, owner_id)
        page_size = clamp_page_limit(limit)
        order = self.turn_order.get(conversation_id, [])
        turns_map = self.turns.get(conversation_id, {})
        ordered = [turns_map[tid] for tid in reversed(order) if tid in turns_map]
        # Include any turns not in order index.
        for tid, turn in turns_map.items():
            if tid not in order:
                ordered.append(turn)
        if cursor is not None:
            sort, item_id = decode_cursor(cursor)
            try:
                sort_i = int(sort)
            except ValueError as exc:
                raise DomainError(ErrorCode.INVALID_CURSOR, "invalid cursor") from exc
            # order_index is position+1 ascending; page uses reverse order.
            ordered = [
                t
                for t in ordered
                if (order.index(t.id) + 1 if t.id in order else 0) < sort_i
                or ((order.index(t.id) + 1 if t.id in order else 0) == sort_i and t.id < item_id)
            ]
        page = ordered[:page_size]
        next_cursor = None
        if len(ordered) > page_size and page:
            last = page[-1]
            idx = order.index(last.id) + 1 if last.id in order else 0
            next_cursor = encode_cursor(sort=str(idx), id=last.id)
        return Page(items=tuple(self._turn_proj(t) for t in page), next_cursor=next_cursor)

    async def page_messages(
        self,
        conversation_id: UUID,
        owner_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> Page[MessageProjection]:
        await self._require_owned(conversation_id, owner_id)
        page_size = clamp_page_limit(limit)
        messages = list(self.messages.get(conversation_id, {}).values())
        messages.sort(key=lambda m: (m.created_at, m.id), reverse=True)
        if cursor is not None:
            sort, item_id = decode_cursor(cursor)
            sort_dt = datetime.fromisoformat(sort)
            messages = [
                m
                for m in messages
                if m.created_at < sort_dt or (m.created_at == sort_dt and m.id < item_id)
            ]
        page = messages[:page_size]
        next_cursor = None
        if len(messages) > page_size and page:
            last = page[-1]
            next_cursor = encode_cursor(sort=last.created_at.isoformat(), id=last.id)
        return Page(
            items=tuple(
                MessageProjection(
                    id=m.id,
                    turn_id=m.turn_id,
                    role=m.role,
                    text=m.text,
                    sequence=m.sequence,
                    interrupted=m.interrupted,
                    created_at=m.created_at,
                )
                for m in page
            ),
            next_cursor=next_cursor,
        )

    async def page_tools(
        self,
        conversation_id: UUID,
        owner_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> Page[ToolProjection]:
        await self._require_owned(conversation_id, owner_id)
        page_size = clamp_page_limit(limit)
        tools = list(enumerate(self.tools.get(conversation_id, {}).values(), start=1))
        tools.reverse()
        if cursor is not None:
            sort, item_id = decode_cursor(cursor)
            order_index = int(sort)
            tools = [
                item
                for item in tools
                if item[0] < order_index
                or (item[0] == order_index and item[1].id < item_id)
            ]
        page = tools[:page_size]
        next_cursor = None
        if len(tools) > page_size and page:
            order_index, last = page[-1]
            next_cursor = encode_cursor(sort=str(order_index), id=last.id)
        return Page(
            items=tuple(
                ToolProjection(
                    id=t.id,
                    turn_id=t.turn_id,
                    tool_name=t.tool_name,
                    arguments=dict(t.arguments),
                    outcome=t.outcome,
                    exit_status=t.exit_status,
                    paths=t.paths,
                    output_tail=t.output_tail,
                )
                for _, t in page
            ),
            next_cursor=next_cursor,
        )

    async def page_plans(
        self,
        conversation_id: UUID,
        owner_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> Page[PlanProjection]:
        await self._require_owned(conversation_id, owner_id)
        page_size = clamp_page_limit(limit)
        plans = list(enumerate(self.plans.get(conversation_id, {}).values(), start=1))
        plans.reverse()
        if cursor is not None:
            sort, item_id = decode_cursor(cursor)
            order_index = int(sort)
            plans = [
                item
                for item in plans
                if item[0] < order_index
                or (item[0] == order_index and item[1].id < item_id)
            ]
        page = plans[:page_size]
        next_cursor = None
        if len(plans) > page_size and page:
            order_index, last = page[-1]
            next_cursor = encode_cursor(sort=str(order_index), id=last.id)
        return Page(
            items=tuple(
                PlanProjection(id=plan.id, turn_id=plan.turn_id, items=plan.items)
                for _, plan in page
            ),
            next_cursor=next_cursor,
        )

    async def page_activity(
        self,
        conversation_id: UUID,
        owner_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> Page[ActivityProjection]:
        await self._require_owned(conversation_id, owner_id)
        page_size = clamp_page_limit(limit)
        activities = list(self.activities.get(conversation_id, {}).values())
        activities.sort(key=lambda a: (a.created_at, a.id), reverse=True)
        if cursor is not None:
            sort, item_id = decode_cursor(cursor)
            sort_dt = datetime.fromisoformat(sort)
            activities = [
                a
                for a in activities
                if a.created_at < sort_dt or (a.created_at == sort_dt and a.id < item_id)
            ]
        page = activities[:page_size]
        next_cursor = None
        if len(activities) > page_size and page:
            last = page[-1]
            next_cursor = encode_cursor(sort=last.created_at.isoformat(), id=last.id)
        return Page(
            items=tuple(
                ActivityProjection(
                    id=a.id,
                    conversation_id=a.conversation_id,
                    parent_turn_id=a.parent_turn_id,
                    parent_activity_id=a.parent_activity_id,
                    status=a.status,
                    title=a.title,
                    summary=a.summary,
                    created_at=a.created_at,
                    completed_at=a.completed_at,
                )
                for a in page
            ),
            next_cursor=next_cursor,
        )

    async def page_pending_interactions(
        self,
        conversation_id: UUID,
        owner_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> Page[InteractionProjection]:
        await self._require_owned(conversation_id, owner_id)
        page_size = clamp_page_limit(limit)
        items = [
            self._interaction_proj(i)
            for i in self.interactions.get(conversation_id, {}).values()
            if i.status in {InteractionStatus.PENDING, InteractionStatus.DRAFT}
        ]
        items.sort(key=lambda i: (i.created_at, i.id))
        if cursor is not None:
            sort, item_id = decode_cursor(cursor)
            sort_dt = datetime.fromisoformat(sort)
            items = [
                i
                for i in items
                if i.created_at > sort_dt or (i.created_at == sort_dt and i.id > item_id)
            ]
        page = items[:page_size]
        next_cursor = None
        if len(items) > page_size and page:
            last = page[-1]
            next_cursor = encode_cursor(sort=last.created_at.isoformat(), id=last.id)
        return Page(items=tuple(page), next_cursor=next_cursor)

    async def commit_facade_mutation(
        self,
        conversation_id: UUID,
        owner_id: str,
        expected_version: int,
        state: ConversationState,
        events: Sequence[ConversationEvent],
        commands: Sequence[Command] = (),
        interaction_answers: Sequence[InteractionAnswer] = (),
    ) -> Sequence[ConversationEvent]:
        current = self.states.get(conversation_id)
        if current is None or current.conversation.owner_id != owner_id:
            raise _not_found("conversation")
        # Allow soft-delete mutation when currently not deleted.
        if current.conversation.deleted_at is not None and state.conversation.deleted_at is None:
            raise _not_found("conversation")
        if current.conversation.version != expected_version:
            raise DomainError(
                ErrorCode.OPTIMISTIC_CONFLICT,
                "optimistic concurrency conflict",
                details={
                    "expected": expected_version,
                    "actual": current.conversation.version,
                },
            )
        if state.conversation.owner_id != owner_id:
            raise _not_found("conversation")
        stored = self.events.setdefault(conversation_id, [])
        for event in events:
            stored.append(event)
        self.states[conversation_id] = state
        for command in commands:
            self.commands[command.id] = command
            if command.status == CommandStatus.ACCEPTED and command.id not in self.accepted_queue:
                self.accepted_queue.append(command.id)
        for answer in interaction_answers:
            self.interaction_answers.setdefault(answer.interaction_id, answer)
        self._index_state_projections(state)
        self._refresh_search(state)
        return tuple(events)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _require_owned(self, conversation_id: UUID, owner_id: str) -> ConversationState:
        state = self.states.get(conversation_id)
        if (
            state is None
            or state.conversation.owner_id != owner_id
            or state.conversation.deleted_at is not None
        ):
            raise _not_found("conversation")
        return state

    def _harness_proj(self, harness: HarnessInstance) -> HarnessProjection:
        return HarnessProjection(
            id=harness.id,
            owner_id=harness.owner_id,
            name=harness.name,
            kind=harness.kind,
            configuration=harness.configuration,
            created_at=harness.created_at,
        )

    def _shell(self, state: ConversationState) -> ConversationShell:
        conv = state.conversation
        binding = state.binding
        pending = any(
            i.status in {InteractionStatus.PENDING, InteractionStatus.DRAFT}
            for i in state.interactions.values()
        )
        return ConversationShell(
            id=conv.id,
            title=conv.display_title,
            status=conv.status,
            harness_kind=binding.kind if binding else None,
            model=binding.configuration.model if binding else None,
            mode=binding.configuration.mode if binding else None,
            has_pending_interactions=pending,
            pinned_at=conv.pinned_at,
            archived_at=conv.archived_at,
            snoozed_until=conv.snoozed_until,
            updated_at=conv.updated_at,
            latest_activity_at=conv.updated_at,
        )

    def _turn_proj(self, turn: Turn) -> TurnProjection:
        return TurnProjection(
            id=turn.id,
            conversation_id=turn.conversation_id,
            status=turn.status,
            user_message_id=turn.user_message_id,
            command_id=turn.command_id,
            created_at=turn.created_at,
            started_at=turn.started_at,
            completed_at=turn.completed_at,
            terminal_reason=turn.terminal_reason,
        )

    def _interaction_proj(self, interaction: PendingInteraction) -> InteractionProjection:
        return InteractionProjection(
            id=interaction.id,
            kind=interaction.kind,
            status=interaction.status,
            turn_id=interaction.turn_id,
            request=interaction.request,
            draft=interaction.draft,
            created_at=interaction.created_at,
        )

    def _index_state_projections(self, state: ConversationState) -> None:
        cid = state.conversation.id
        turns = self.turns.setdefault(cid, {})
        order = self.turn_order.setdefault(cid, [])
        for turn in (state.active_turn, state.queued_turn):
            if turn is None:
                continue
            turns[turn.id] = turn
            if turn.id not in order:
                order.append(turn.id)
            # Synthesize a user message from queued text when present.
            if (
                turn.status is TurnStatus.QUEUED
                and state.queued_user_text
                and turn.user_message_id is None
            ):
                from uuid import uuid4

                from talktoharnesses.domain.enums import MessageRole

                msg_id = uuid4()
                turn = turn.model_copy(update={"user_message_id": msg_id})
                turns[turn.id] = turn
                self.messages.setdefault(cid, {})[msg_id] = Message(
                    id=msg_id,
                    turn_id=turn.id,
                    role=MessageRole.USER,
                    text=state.queued_user_text,
                    created_at=turn.created_at,
                )
        for interaction in state.interactions.values():
            self.interactions.setdefault(cid, {})[interaction.id] = interaction
        for activity in state.activities.values():
            self.activities.setdefault(cid, {})[activity.id] = activity

    def _refresh_search(self, state: ConversationState) -> None:
        cid = state.conversation.id
        messages = self.messages.get(cid, {}).values()
        tools = self.tools.get(cid, {}).values()
        self.search_docs[cid] = build_search_document_from_parts(
            title=state.conversation.display_title,
            message_texts=[m.text for m in messages],
            tool_names=[t.tool_name for t in tools],
            tool_arguments=[dict(t.arguments) for t in tools],
            tool_paths=[p for t in tools for p in t.paths],
            tool_output_tails=[t.output_tail for t in tools if t.output_tail],
        )

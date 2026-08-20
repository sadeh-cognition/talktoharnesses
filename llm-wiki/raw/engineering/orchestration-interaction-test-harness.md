# Orchestration interaction test harness

- **Status:** Proposed
- **Date:** 2026-08-20
- **Inspected commit:** bffc9566181f4309b7f22d446dc950451d78a0d1

## Context

TalkToHarnesses already implements the provider-neutral interaction path:

1. A `HarnessAdapter` yields `HarnessInteractionRequest` or
   `InteractionRequestedPayload` from `events()`.
2. `CommandProcessor._on_harness_event` force-flushes the delta batcher and
   calls `InteractionBroker.accept_request`.
3. The broker commits `interaction_requested`, publishes, then evaluates
   owner-scoped rules or waits for `resolve_interaction`.
4. After the resolution event is published, an `answer_interaction` command
   is released.
5. The processor claims that command and calls
   `adapter.answer_interaction(session, answer)`.
6. The adapter continues the turn, typically by emitting
   `TurnCompletedPayload`.

`TalkToHarnessesService.start(worker_id)` already runs coordinator recovery
and the real claim loop. Unit tests start the service on `MemoryPersistence`.

A comparison with T3's provider-orchestration tests (in-process
`TestProviderAdapter` plus `OrchestrationEngineHarness`; not T3 UI, HTTP, or
provider-maintenance tests) shows that T3 drives this loop through one
programmable fake adapter and wait-for-receipt helpers. TalkToHarnesses
covers the same surfaces in fragments:

- Contract fakes in `tests/contract/fakes.py` wrap real adapter classes and
  only complete a turn. They do not emit approvals or questions.
- Runtime `FakeAdapter` in `tests/runtime/conftest.py` can seed
  `InteractionRequestedPayload`, but `answer_interaction` is a no-op and
  events dump in one shot. `seed_reply="interaction"` is unused.
- `_InteractionAdapter` in
  `tests/unit/application/test_command_processor_interactions.py` queues
  events and records answers against a stub runtime, then sleep-polls the
  publisher.
- `_Phase10Adapter` in `tests/e2e/test_phase10_definition_of_done.py` is the
  same idea with Django and a real `RuntimeManager`, but tests poke
  `svc._started` / coordinator health flags and manually
  `claim_commands` plus `processor._execute_command`.
- Phase 6 e2e in `tests/e2e/test_phase6_approvals_gate.py` constructs a
  `PendingInteraction` and calls `service._broker.accept_request` with an
  empty `AdapterRegistry`. No adapter emit, no `answer_interaction` delivery.

Live gates in `tests/live` already drive real binaries over HTTP/SSE. They
are opt-in, slow, and not a substitute for a deterministic closed loop.

## Decision

Add a test-only orchestration harness under `tests/orchestration/`:

- `ScriptedAdapter`: provider-neutral `HarnessAdapter`, `kind=GROK`,
  `sdk_managed=True`. Factory-owned script deque (RuntimeManager constructs
  a fresh adapter per start/resume). `queue_turn(*events)` emits on the next
  `submit()`. Empty deque on submit fails the test. An interaction event
  pauses the remainder until `answer_interaction` for that id; then the rest
  of the script (typically `TurnCompletedPayload`) is emitted. `interrupt()`
  records the call and emits `TurnInterruptedPayload`. Record submissions,
  answers, interrupts, start, and resume.
- `ServiceHarness`: `MemoryPersistence`, capturing
  `CommittedEventPublisher`, `RuntimeManager`, `TalkToHarnessesService`.
  Enter calls public `service.start()`; exit calls `service.stop()`. No
  private claim-loop bypass. Wait helpers poll `replay_events` or adapter
  recordings; timeout is a test failure, not a pass condition.
- Four pytest-asyncio tests: manual approval closed loop; auto-rule closed
  loop; first-write-wins (one answer delivered); interrupt during an open
  interaction.

Do not subclass runtime `FakeAdapter`. Do not teach contract fakes to emit
native mid-turn approval frames. Provider mapping of `can_use_tool` / ACP
permission / OpenCode ask stays in `tests/unit/providers`.

## Non-goals

- Scripted subprocess mock-peer for ACP/Codex children.
- Production `DrainableWorker` / idle-queue drain API on `CommandProcessor`.
- T3 git-checkpoint/rollback, UI, HTTP/CORS, or provider-maintenance tests.
- Replacing live gates.
- Migrating phase 6/8/10 e2e onto the new harness in the same change.
- Claude `ALLOW_SESSION` / empty-MCP `canUseTool` adapter unit cases.

## Consequences

- pytest `testpaths = ["tests"]` picks up `tests/orchestration/` with no
  Makefile or CI change.
- No `src/` change is expected. If `service.start()` cannot run
  submit → claim → pump on `MemoryPersistence` without private pokes, fix
  the harness first.
- After the suite exists, follow-ups may replace `_Phase10Adapter` /
  `_InteractionAdapter`, add a Django/HTTP variant, and cite the new tests
  from requirement Test evidence sections.

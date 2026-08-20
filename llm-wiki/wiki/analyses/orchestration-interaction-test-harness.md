---
type: analysis
title: Orchestration Interaction Test Harness
status: proposed
audiences:
  - developer
tags:
  - type/analysis
  - audience/developer
  - capability/interactions
  - status/proposed
last_verified: 2026-08-20
verified_against_commit: bffc9566181f4309b7f22d446dc950451d78a0d1
---

# Orchestration Interaction Test Harness

The public interaction contract is implemented. What is missing is a single in-process test that drives a running command worker from adapter emit through broker resolution to `answer_interaction` and turn continue. This analysis proposes that harness. It is not present in the tree at the inspected commit.

T3's provider-orchestration tests are an external comparison only: an in-process fake adapter plus wait-for-receipt helpers. T3 UI, HTTP, and provider-maintenance tests are out of scope.

## Context

[Adapter protocol](../interfaces/adapter-protocol.md) adapters yield `HarnessInteractionRequest`. [Python application facade](../interfaces/python-application-facade.md) owns the command worker and [interaction broker](../requirements/resolve-approvals-and-structured-questions.md). `TalkToHarnessesService.start` already runs recovery and the claim loop on `MemoryPersistence`.

Existing tests cover pieces of that path, not the loop:

- Contract fakes in `tests/contract/fakes.py` wrap real adapters and only complete a turn.
- Runtime `FakeAdapter` in `tests/runtime/conftest.py` can seed an interaction payload, but `answer_interaction` is a no-op.
- `_InteractionAdapter` in `tests/unit/application/test_command_processor_interactions.py` queues events against a stub runtime.
- `_Phase10Adapter` in `tests/e2e/test_phase10_definition_of_done.py` uses a real `RuntimeManager` but bypasses the claim loop with private `_execute_command`.
- Phase 6 e2e in `tests/e2e/test_phase6_approvals_gate.py` constructs a `PendingInteraction` and calls `accept_request` with an empty adapter registry.

Live gates in `tests/live` prove real binaries over HTTP/SSE. They are opt-in and not a substitute for a deterministic closed loop.

## Proposal

Add test-only code under `tests/orchestration/`:

- `ScriptedAdapter`: a provider-neutral `HarnessAdapter` (`kind=GROK`, `sdk_managed=True`) with a factory-owned script deque. `queue_turn` events emit on the next `submit()`. An interaction event pauses until `answer_interaction`; then the rest of the script runs. `interrupt()` records the call and emits `TurnInterruptedPayload`. Submit with an empty deque fails the test.
- `ServiceHarness`: `MemoryPersistence`, a capturing publisher, `RuntimeManager`, and `TalkToHarnessesService`. Enter calls public `service.start()`; exit calls `service.stop()`. Wait helpers poll committed events or adapter recordings. Timeout is a failure, not a pass condition.
- Four tests: manual approval closed loop; auto-rule closed loop; first-write-wins (one answer delivered); interrupt during an open interaction.

Do not subclass runtime `FakeAdapter`. Do not extend contract fakes to emit native mid-turn approvals. Provider permission mapping stays in `tests/unit/providers`.

## Out of scope

- Scripted subprocess mock-peer, production drain APIs, live-gate replacement, and migrating phase 6/8/10 e2e onto this harness in the same change.
- After the suite exists, follow-ups may replace the duplicated adapters and cite `tests/orchestration/` from requirement Test evidence.

## Related

- [Engineering orchestration interaction test harness source](../../raw/engineering/orchestration-interaction-test-harness.md)
- [Testing guidelines](../operations/testing-guidelines.md)
- [Adapter protocol](../interfaces/adapter-protocol.md)
- [Python application facade](../interfaces/python-application-facade.md)
- [Resolve approvals and structured questions](../requirements/resolve-approvals-and-structured-questions.md)
- [Apply approval rules](../requirements/apply-approval-rules.md)
- [Resolve a pending approval](../journeys/resolve-a-pending-approval.md)
- [Approvals and structured questions](../capabilities/approvals-and-questions.md)

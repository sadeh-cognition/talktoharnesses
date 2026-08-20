---
type: requirement
title: Steer, Interrupt, and Switch Harness
status: implemented
audiences:
  - product
  - developer
tags:
  - type/requirement
  - capability/adapters
  - status/implemented
last_verified: 2026-08-20
verified_against_commit: bb3d2b755500fc663816d6cbd1a7cd7947a8920b
---

# Steer, Interrupt, and Switch Harness

## Intent

An owner can steer a running turn when the adapter supports it, interrupt work, and switch a conversation to another owned harness while keeping one conversation identity.

## Current behavior

`POST .../steer`, `POST .../interrupt`, and `POST .../switch` enqueue durable commands. Steer fails closed when `supports_steer` is false. Interrupt is published for adapters that implement it. Switch validates the target harness, closes the active binding, and opens a new binding on the same conversation.

## Gap

No gap remains against adapter-owned capability flags. Nested activity is unpublished for adapters that do not emit `activity_started`.

## Acceptance criteria

- Steer is rejected when the adapter does not support it.
- Interrupt stops the in-flight turn through the adapter and records command settlement.
- Switch keeps one conversation id, one active binding, and continues event sequence.
- Unsupported operations fail closed rather than no-op success.

## Implementation evidence

- `src/talktoharnesses/application/service.py` (`steer`, `interrupt`, `switch_harness`)
- `src/talktoharnesses/providers/adapter.py`
- `src/talktoharnesses/providers/compatibility.py`

## Test evidence

- `tests/e2e/test_phase8_switch_gate.py`
- `tests/unit/providers/test_adapter_matrix_enforcement.py`
- `tests/live/test_*_live.py`

## Related

- [Unified harness adapters](../capabilities/unified-harness-adapters.md)
- [Turn and command](../domain/turn-and-command.md)
- [Create and manage conversations](create-and-manage-conversations.md)

---
type: requirement
title: Queue and Edit Prompts
status: implemented
audiences:
  - product
  - developer
tags:
  - type/requirement
  - capability/conversations
  - status/implemented
last_verified: 2026-08-20
verified_against_commit: bb3d2b755500fc663816d6cbd1a7cd7947a8920b
---

# Queue and Edit Prompts

## Intent

While a turn is active, an owner can queue a follow-up prompt, replace the queued text, or cancel the queued prompt without starting a second concurrent turn.

## Current behavior

A second submit while a turn is running queues. `PATCH /conversations/{id}/queued-prompt` replaces the entire queue text. `DELETE` cancels the queued prompt and returns the command or 204 when nothing was queued. Coalescing preserves order of distinct queued work except when edit replaces the queue.

## Gap

No gap remains against the one-active-turn and queue contract.

## Acceptance criteria

- Only one turn is active.
- Queued prompts run after the active turn completes.
- Edit replaces the queued prompt in full.
- Cancel removes the queued prompt without interrupting the active turn.

## Implementation evidence

- `src/talktoharnesses/application/service.py` (`edit_queued_prompt`, `cancel_queued_prompt`, `submit_turn`)
- `src/talktoharnesses/domain/transitions.py`

## Test evidence

- `tests/property/test_one_active_turn.py`
- `tests/property/test_queued_prompt.py`

## Related

- [Submit turns and stream events](submit-turns-and-stream-events.md)
- [Persistent conversations and turns](../capabilities/persistent-conversations.md)
- [Turn and command](../domain/turn-and-command.md)

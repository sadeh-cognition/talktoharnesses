---
type: requirement
title: Resolve Approvals and Structured Questions
status: implemented
audiences:
  - product
  - developer
tags:
  - type/requirement
  - capability/interactions
  - status/implemented
last_verified: 2026-08-20
verified_against_commit: bb3d2b755500fc663816d6cbd1a7cd7947a8920b
---

# Resolve Approvals and Structured Questions

## Intent

When a harness requests an approval or structured question, the owner can list the pending interaction, optionally save a draft, and resolve it so the same turn continues.

## Current behavior

Pending interactions are listed per conversation. Approval resolution accepts `allow_once`, `allow_session`, `deny`, or `cancel`. Structured questions accept a canonical `answers` object. Resolution is a durable command. The first accepted answer wins. Yolo harnesses do not publish approval interactions.

## Gap

No gap remains against the documented interaction contract.

## Acceptance criteria

- Pending approvals and structured questions are owner-visible on the conversation.
- Resolve submits a decision or answers and resumes the turn.
- Duplicate resolves do not apply a second answer.
- Draft updates persist without resolving.
- Unsupported interaction kinds fail harness execution rather than silently succeeding.

## Implementation evidence

- `src/talktoharnesses/application/service.py` (`list_pending_interactions`, `update_interaction_draft`, `resolve_interaction`)
- `src/talktoharnesses/application/interaction_broker.py`
- `src/talktoharnesses/domain/questions.py`
- `src/talktoharnesses/django/api/routes.py`

## Test evidence

- `tests/e2e/test_phase6_approvals_gate.py`
- `tests/property/test_interaction_resolution.py`
- `tests/unit/domain/test_questions.py`
- `tests/unit/django/test_approval_api.py`

## Related

- [Approvals and structured questions](../capabilities/approvals-and-questions.md)
- [Apply approval rules](apply-approval-rules.md)
- [Interaction](../domain/interaction.md)
- [Resolve a pending approval](../journeys/resolve-a-pending-approval.md)

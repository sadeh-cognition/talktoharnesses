---
type: requirement
title: Resolve Approvals and Structured Questions
status: partially-implemented
audiences:
  - product
  - developer
tags:
  - type/requirement
  - capability/interactions
  - status/partially-implemented
last_verified: 2026-09-05
verified_against_commit: 92bdf81138628204f7b58df5f1f80545abdbbde3
---

# Resolve Approvals and Structured Questions

## Intent

When a harness requests an approval or structured question, the owner can list the pending interaction, optionally save a draft, and resolve it so the same turn continues.

## Current behavior

Pending interactions are listed per conversation. Approval resolution accepts `allow_once`, `allow_session`, `deny`, or `cancel`. Structured questions accept a canonical `answers` object. Resolution is a durable command. The first accepted answer wins. Yolo harnesses do not publish approval interactions.

Muse approval receipt requests and approval notifications remain distinct.
Decision delivery is deduplicated before IO; repeated answers share the original
outcome. SDK-style retries retain the command identity and apply only to explicit
overload/backpressure errors. Internal errors remain visible.

## Gap

Muse Code `1.0.3-R2198.1` can reject approval decisions after resume with
MSP `-32603` and an approval-ledger durability error. This reproduces with Meta's
SDK outside TTH and Docker. SDK-aligned handling passes protocol tests, but the
live gate detects answer commands with unknown native outcomes even when turns
finish and enough interaction requests appear. Deterministic closed-loop test evidence (adapter request through resolve through `answer_interaction` under a running command worker) is proposed in [Orchestration Interaction Test Harness](../analyses/orchestration-interaction-test-harness.md) and is not present yet.

## Acceptance criteria

- Pending approvals and structured questions are owner-visible on the conversation.
- Resolve submits a decision or answers and resumes the turn.
- Duplicate resolves do not apply a second answer.
- Draft updates persist without resolving.
- Unsupported interaction kinds fail harness execution rather than silently succeeding.

## Implementation evidence

- `tth-muse/src/tth_muse/harness/` (Muse Code MSP integration)

- `src/talktoharnesses/application/service.py` (`list_pending_interactions`, `update_interaction_draft`, `resolve_interaction`)
- `src/talktoharnesses/application/interaction_broker.py`
- `src/talktoharnesses/domain/questions.py`
- `src/talktoharnesses/django/api/routes.py`

## Test evidence

- `tth-muse/tests/test_muse.py`
- `tests/live/test_muse_sandbox_live.py`

- `tests/e2e/test_phase6_approvals_gate.py`
- `tests/property/test_interaction_resolution.py`
- `tests/unit/domain/test_questions.py`
- `tests/unit/django/test_approval_api.py`

`tests/orchestration/` is proposed by [Orchestration Interaction Test Harness](../analyses/orchestration-interaction-test-harness.md) and is not in the tree at the inspected commit.

## Related

- [Approvals and structured questions](../capabilities/approvals-and-questions.md)
- [Apply approval rules](apply-approval-rules.md)
- [Interaction](../domain/interaction.md)
- [Resolve a pending approval](../journeys/resolve-a-pending-approval.md)
- [Orchestration Interaction Test Harness](../analyses/orchestration-interaction-test-harness.md)

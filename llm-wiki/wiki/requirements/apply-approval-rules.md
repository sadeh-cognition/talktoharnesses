---
type: requirement
title: Apply Approval Rules
status: implemented
audiences:
  - product
  - developer
tags:
  - type/requirement
  - capability/interactions
  - status/implemented
last_verified: 2026-08-20
verified_against_commit: bffc9566181f4309b7f22d446dc950451d78a0d1
---

# Apply Approval Rules

## Intent

An owner can create persistent allow or deny rules that automatically resolve matching approval requests for a user, harness instance, or conversation scope.

## Current behavior

CRUD endpoints manage `ApprovalRule` records. Matching uses canonical matcher fields. Matching rules auto-resolve without a second human action and emit the same event shape as manual resolve except for the automatic path. Interaction audits record outcomes. Yolo harnesses do not participate in package rules.

## Gap

No product gap remains against the Phase 6 approval-rule contract. Deterministic closed-loop test evidence (adapter request through rule auto-resolve through `answer_interaction` under a running command worker) is proposed in [Orchestration Interaction Test Harness](../analyses/orchestration-interaction-test-harness.md) and is not present yet.

## Acceptance criteria

- Rules are owner-scoped and cannot match another owner's interactions.
- Allow and deny rules auto-resolve matching pending approvals.
- Manual and rule-driven event payloads match except for the automatic marker.
- Resolve may optionally create a rule in the same request.

## Implementation evidence

- `src/talktoharnesses/application/service.py` (approval rule methods)
- `src/talktoharnesses/domain/approval_matching.py`
- `src/talktoharnesses/django/api/routes.py`

## Test evidence

- `tests/e2e/test_phase6_approvals_gate.py`
- `tests/unit/django/test_approval_api.py`
- `tests/unit/domain/test_approval_matching.py`

`tests/orchestration/` is proposed by [Orchestration Interaction Test Harness](../analyses/orchestration-interaction-test-harness.md) and is not in the tree at the inspected commit.

## Related

- [Resolve approvals and structured questions](resolve-approvals-and-structured-questions.md)
- [Approvals and structured questions](../capabilities/approvals-and-questions.md)
- [Interaction](../domain/interaction.md)
- [Orchestration Interaction Test Harness](../analyses/orchestration-interaction-test-harness.md)

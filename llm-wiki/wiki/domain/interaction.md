---
type: domain
title: Interaction
status: implemented
audiences:
  - product
  - developer
tags:
  - type/domain
  - capability/interactions
last_verified: 2026-08-20
verified_against_commit: bb3d2b755500fc663816d6cbd1a7cd7947a8920b
---

# Interaction

An interaction is an approval or structured question with status pending, draft, submitted, resolved, or cancelled.

Approvals use `allow_once`, `allow_session`, `deny`, or `cancel`. Persistent rules use allow or deny and a matcher scoped to user, harness instance, or conversation. Structured questions use a canonical answers object. Audits record automatic and manual outcomes.

## Related

- [Approvals and structured questions](../capabilities/approvals-and-questions.md)
- [Resolve approvals and structured questions](../requirements/resolve-approvals-and-structured-questions.md)
- [Apply approval rules](../requirements/apply-approval-rules.md)
- [Turn and command](turn-and-command.md)

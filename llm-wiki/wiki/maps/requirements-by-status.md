---
type: map
title: Requirements by Status
status: maintained
audiences:
  - product
  - developer
tags:
  - type/map
  - audience/product
  - audience/developer
last_verified: 2026-08-30
verified_against_commit: 78003994d9fe93108ce5a6bc3591ab2e2ef904d9
---

# Requirements by Status

Plugin-free delivery view of the public contracts documented in this vault.

## Partially implemented

- [Report harness token usage](../requirements/report-harness-token-usage.md):
  Cursor Agent does not currently expose token usage through ACP.

## Implemented

- [Probe and configure harnesses](../requirements/probe-and-configure-harnesses.md)
- [Create and manage conversations](../requirements/create-and-manage-conversations.md)
- [Submit turns and stream events](../requirements/submit-turns-and-stream-events.md)
- [Resolve approvals and structured questions](../requirements/resolve-approvals-and-structured-questions.md)
- [Steer, interrupt, and switch harness](../requirements/steer-interrupt-and-switch-harness.md)
- [Queue and edit prompts](../requirements/queue-and-edit-prompts.md)
- [Apply approval rules](../requirements/apply-approval-rules.md)
- [Search conversations](../requirements/search-conversations.md)
- [Retain and prune transcripts](../requirements/retain-and-prune-transcripts.md)
- [Export and import transcripts](../requirements/export-and-import-transcripts.md)
- [Authenticate with JWT](../requirements/authenticate-with-jwt.md)
- [Host Django ASGI with readiness](../requirements/host-django-asgi-with-readiness.md)

## Historical decisions

- [Strict compatibility decision](../decisions/strict-compatibility.md) is superseded by [floor-and-probe compatibility](../decisions/floor-and-probe-compatibility.md).

## Related

- [Product overview](product-overview.md)
- [TalkToHarnesses overview](../overview.md)

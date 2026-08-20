---
type: journey
title: Resolve a Pending Approval
status: implemented
audiences:
  - product
  - developer
tags:
  - type/journey
  - capability/interactions
  - status/implemented
last_verified: 2026-08-20
verified_against_commit: bb3d2b755500fc663816d6cbd1a7cd7947a8920b
---

# Resolve a Pending Approval

This journey traces a harness tool-permission pause through listing, optional rule match, resolve, and turn resume.

## 1. A turn requests approval

The adapter emits an interaction request. The conversation moves to waiting. SSE publishes the pending interaction. Yolo harnesses skip this path.

## 2. List or auto-match

The client lists pending interactions, or an owner-scoped approval rule auto-resolves. Requirements: [Resolve approvals and structured questions](../requirements/resolve-approvals-and-structured-questions.md), [Apply approval rules](../requirements/apply-approval-rules.md).

## 3. Resolve

The client posts a decision (`allow_once`, `allow_session`, `deny`, `cancel`) and may create a rule in the same request. First accepted answer wins.

## 4. Resume

The adapter receives the answer, the turn continues, and the SSE stream is followed with `Last-Event-ID`.

## Related

- [Approvals and structured questions](../capabilities/approvals-and-questions.md)
- [Interaction](../domain/interaction.md)
- [Submit turns and stream events](../requirements/submit-turns-and-stream-events.md)

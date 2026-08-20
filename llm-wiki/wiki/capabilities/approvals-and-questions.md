---
type: capability
title: Approvals and Structured Questions
status: implemented
audiences:
  - product
  - developer
tags:
  - type/capability
  - capability/interactions
  - status/implemented
last_verified: 2026-08-20
verified_against_commit: bb3d2b755500fc663816d6cbd1a7cd7947a8920b
---

# Approvals and Structured Questions

Turns can pause for an approval or a structured question. Clients list pending interactions, optionally save a draft, and resolve with a decision or answers object.

## Product value

Hosts keep humans in the loop for tool permission and provider-native questions without moving authority into the client. Owner-scoped approval rules can auto-resolve matching approvals. Yolo harnesses skip approval prompts through provider-native mechanisms; structured questions remain interactive.

## Current implementation

Adapters emit `HarnessInteractionRequest`. The interaction broker admits pending interactions, applies matching rules, and records audits. Resolution is a durable command. First accepted answer wins.

## Requirements

- [Resolve approvals and structured questions](../requirements/resolve-approvals-and-structured-questions.md)
- [Apply approval rules](../requirements/apply-approval-rules.md)

## Related

- [Interaction](../domain/interaction.md)
- [Resolve a pending approval](../journeys/resolve-a-pending-approval.md)
- [Steer, interrupt, and switch harness](../requirements/steer-interrupt-and-switch-harness.md)

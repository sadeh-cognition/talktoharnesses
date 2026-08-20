---
type: domain
title: Turn and Command
status: implemented
audiences:
  - product
  - developer
tags:
  - type/domain
  - capability/conversations
last_verified: 2026-08-20
verified_against_commit: bb3d2b755500fc663816d6cbd1a7cd7947a8920b
---

# Turn and Command

A turn is one prompt execution with status queued, running, waiting, completed, interrupted, failed, or outcome-unknown. Messages, tools, plans, and activities hang off the turn.

A command is the durable control message: submit turn, steer, edit queued, cancel queued, interrupt, answer interaction, or switch harness. Command status moves through accepted, claimed, delivery-started, delivered, settled, coalesced, or outcome-unknown.

## Related

- [Submit turns and stream events](../requirements/submit-turns-and-stream-events.md)
- [Queue and edit prompts](../requirements/queue-and-edit-prompts.md)
- [Conversation](conversation.md)
- [Interaction](interaction.md)

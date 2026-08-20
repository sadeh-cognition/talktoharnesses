---
type: decision
title: Persistence Decision
status: implemented
audiences:
  - developer
tags:
  - type/decision
  - audience/developer
last_verified: 2026-08-20
verified_against_commit: bb3d2b755500fc663816d6cbd1a7cd7947a8920b
sources:
  - raw/engineering/adr-0001-persistence.md
---

# Persistence Decision

Persistence is mandatory for execution. Canonical conversations, turns, messages, interactions, activities, process records, usage, and the event log live in relational models. Native harness state is not durable truth.

The production implementation uses Django ORM with PostgreSQL or production SQLite. A runtime refuses to execute a turn without a persistence implementation.

## Related

- [ADR 0001 source](../../raw/engineering/adr-0001-persistence.md)
- [Persistence and event sequencing](../architecture/persistence-and-event-sequencing.md)
- [Persistent conversations and turns](../capabilities/persistent-conversations.md)

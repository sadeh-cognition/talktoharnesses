# ADR 0001: Persistence

- **Status:** Accepted
- **Date:** 2026-08-07

## Context

Orchestration must survive process restarts, support history APIs, and allow
clients to reconnect without losing committed events. Native harness state is
a resumable execution detail and cannot be the canonical transcript.

## Decision

Persistence is mandatory for execution. Store canonical conversations, turns,
messages, interactions, activities, process records, usage, and related state
in relational models, alongside an append-only conversation event log. The
relational database is authoritative; neither the native harness nor in-memory
runtime state is durable truth. The production implementation uses Django ORM
and supports PostgreSQL and production SQLite.

## Consequences

Later phases define the persistence protocol and Django repositories. A runtime
must refuse to execute a turn when no persistence implementation is configured.

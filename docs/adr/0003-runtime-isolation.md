# ADR 0003: Runtime Isolation

- **Status:** Accepted; execution location superseded by the split-services architecture
- **Date:** 2026-08-07

## Context

Harness turns and event pumps must outlive individual HTTP requests. A live
adapter or process also contains conversation-specific native session state and
cannot be shared safely between conversations.

## Decision

Create one supervised SDK/process runtime per active conversation. Request
handlers never own its lifetime, and disconnecting the last client does not
interrupt work. Durable state and worker ownership remain in the database; the
live runtime is only an execution detail.

The original decision placed harness execution in the Django process boundary.
The later [split-services decision](../../llm-wiki/wiki/decisions/split-services.md)
moved the native runtime into a per-kind split while preserving the
one-runtime-per-conversation rule.

## Consequences

SQLite uses a single-supervisor profile. PostgreSQL may coordinate multiple
workers through transactional claims, renewable leases, and notifications,
without promising transfer of a live stdio connection. API and worker execution
may share the same process/container profile, and no external broker is added.

# ADR 0006: Retention

- **Status:** Accepted
- **Date:** 2026-08-07

## Context

Canonical transcripts, tool output, normalized events, and redacted native
events accumulate over long-running use. Retention must remove expired database
state without deleting project files from disk.

## Decision

An externally scheduled Django management command enforces owner-scoped
calendar-month retention (default six months when no policy is stored). For
active conversations it deletes complete expired turn aggregates, including
their canonical and raw events, while skipping running or background-active
conversations and conversations marked retention-exempt. It cancels expired
waiting interactions, prunes their turns, and rotates the native session after
history removal. Soft-deleted conversations are permanently deleted after the
owner's configured period from `deleted_at`, regardless of exemption.

## Consequences

Title sources are recomputed after pruning. Deletion succeeds even if native
session rotation fails; the binding is then marked for recreation. Preview and
`--dry-run` share cleanup eligibility without mutation. Product detail lives in
[`docs/search-retention-transcripts.md`](../search-retention-transcripts.md).

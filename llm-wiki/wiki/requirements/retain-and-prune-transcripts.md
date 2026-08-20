---
type: requirement
title: Retain and Prune Transcripts
status: implemented
audiences:
  - product
  - developer
tags:
  - type/requirement
  - capability/retention
  - status/implemented
last_verified: 2026-08-20
verified_against_commit: bb3d2b755500fc663816d6cbd1a7cd7947a8920b
sources:
  - raw/engineering/adr-0006-retention.md
  - raw/engineering/search-retention-transcripts.md
---

# Retain and Prune Transcripts

## Intent

An externally scheduled command removes expired database state without deleting project files. Active and retention-exempt conversations are skipped. Soft-deleted conversations are permanently deleted after the owner's period from `deleted_at`.

## Current behavior

Default policy is six calendar months when none is stored. Cleanup deletes complete expired turn aggregates including canonical and raw events, cancels expired waiting interactions, and rotates the native session after history removal. Preview and `--dry-run` share eligibility without mutation. Session rotation failure still succeeds deletion and marks the binding for recreation.

## Gap

No gap remains against ADR 0006.

## Acceptance criteria

- Owner-scoped month policy is readable and replaceable.
- Preview lists eligible work without mutation.
- Running and background-active conversations are skipped.
- Retention-exempt conversations skip history prune but not permanent delete of already soft-deleted rows after the period.
- Workspace files are never deleted.

## Implementation evidence

- `src/talktoharnesses/application/retention.py`
- `src/talktoharnesses/django/management/commands/talktoharnesses_cleanup.py`
- `src/talktoharnesses/application/service.py` (retention policy methods)

## Test evidence

- `tests/unit/django/test_cleanup_command.py`
- `tests/test_docs_ops.py::test_documented_cleanup_command_is_importable`
- `tests/unit/application/test_retention_cleanup.py`
- `tests/unit/application/test_retention_cutoff.py`

## Related

- [Retention decision](../decisions/retention.md)
- [Search, retention, and transcripts](../capabilities/search-retention-transcripts.md)
- [Search conversations and apply retention](../journeys/search-conversations-and-apply-retention.md)

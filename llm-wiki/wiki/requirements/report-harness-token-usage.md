---
type: requirement
title: Report Harness Token Usage
status: implemented
audiences:
  - product
  - developer
tags:
  - type/requirement
  - capability/adapters
  - status/implemented
last_verified: 2026-08-21
verified_against_commit: c996cbcd23b7cbf4f6b4d70422ab17ce715661bf
sources:
  - raw/product/harness-token-usage-requirements.md
  - raw/engineering/live-testing-token-usage.md
---

# Report Harness Token Usage

## Intent

Consumers can observe meaningful provider-reported token usage for successful
turns from every supported harness without interpreting native provider events.

## Current behavior

Grok and Cursor normalize their native completion or ACP usage events. Codex
normalizes the per-turn portion of its thread token update. Claude aggregates
per-model usage with a top-level fallback. OpenCode aggregates unique
`step-finish` parts and reconciles message history before terminal. Prime Agent
aggregates assistant-message usage. All six produce the existing canonical
`usage_updated` payload before a successful turn terminal.

The shared live gate requires meaningful usage for each provider's create and
resume turns. Categories omitted by a provider remain absent, and existing
transcripts are not backfilled.

## Gap

No gap remains against the approved harness token-usage contract.

## Acceptance criteria

- Grok, Cursor, Codex, Claude Code, OpenCode, and Prime Agent emit canonical
  usage for newly executed successful turns.
- Usage is attributed to the active turn and precedes its terminal event.
- Every reported token value is a nonnegative integer and at least one value is
  positive; unsupported categories may remain absent.
- TalkToHarnesses does not synthesize missing token categories or add cost
  normalization as part of this requirement.
- Every provider live gate enforces the usage rule for its successful create
  and resume turns.

## Implementation evidence

- `src/talktoharnesses/providers/` usage normalizers for ACP, Grok, Codex,
  Claude, OpenCode, and Prime Agent
- `src/talktoharnesses/domain/events.py` (`UsageUpdatedPayload`)
- `tests/live/helpers.py` (shared create/resume usage gate)

## Test evidence

- `tests/unit/providers/acp/test_normalizer.py`
- `tests/unit/providers/grok/test_normalizer.py`
- `tests/unit/providers/codex/test_adapter.py`
- `tests/unit/providers/claude/test_normalizer.py`
- `tests/unit/providers/opencode/`
- `tests/unit/providers/prime_agent/`
- `tests/unit/live/test_helpers.py`
- `tests/live/test_grok_live.py`, `test_cursor_live.py`, `test_codex_live.py`,
  `test_claude_live.py`, `test_opencode_live.py`, and
  `test_prime_agent_live.py`

## Related

- [Unified harness adapters](../capabilities/unified-harness-adapters.md)
- [Provider adapters](../architecture/provider-adapters.md)
- [Conversation event](../domain/conversation-event.md)
- [Testing guidelines](../operations/testing-guidelines.md)
- [Compatibility and adapters](../maps/compatibility-and-adapters.md)

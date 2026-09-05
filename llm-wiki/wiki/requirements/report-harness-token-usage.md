---
type: requirement
title: Report Harness Token Usage
status: partially-implemented
audiences:
  - product
  - developer
tags:
  - type/requirement
  - capability/adapters
  - status/partially-implemented
last_verified: 2026-09-05
verified_against_commit: 92bdf81138628204f7b58df5f1f80545abdbbde3
sources:
  - raw/product/harness-token-usage-requirements.md
  - raw/engineering/live-testing-token-usage.md
  - raw/engineering/cursor-acp-token-usage-limitation.md
---

# Report Harness Token Usage

## Intent

Consumers can observe meaningful provider-reported token usage for successful
turns from every supported harness without interpreting native provider events.

## Current behavior

Grok normalizes its native ACP usage events. Codex normalizes the per-turn
portion of its thread token update. Claude aggregates per-model usage with a
top-level fallback. OpenCode aggregates unique `step-finish` parts and
reconciles message history before terminal. Prime Agent aggregates
assistant-message usage. Muse Code accumulates MSP per-completion usage for
the active turn, retaining the provider's counted-once prompt/total fields and
using terminal-only reported counts when needed. These six providers produce the existing canonical
`usage_updated` payload before a successful turn terminal.

The Cursor adapter recognizes ACP `usage_update` notifications and
`PromptResponse.usage` terminal data, but verified Cursor Agent releases do not
send either form. Cursor turns therefore complete without a canonical usage
event.

The shared live gate requires meaningful usage for each provider's create and
resume turns. The Cursor gate currently fails that assertion. Categories
omitted by a provider remain absent, and existing transcripts are not
backfilled.

## Gap

Cursor Agent's ACP transport does not currently populate
`PromptResponse.usage` or emit `usage_update`. Cursor staff confirmed the
missing response data as a bug for `2026.05.09-0afadcc` and the missing session
update as a feature gap for `2026.07.09-a3815c0`. TalkToHarnesses live checks
observed the same limitation on `2026.08.04-aaa8809`,
`2026.08.11-e8db854`, and `2026.08.25-3e8eec8`.

Headless `stream-json` token totals are not an ACP substitute for the existing
Cursor adapter. Because TalkToHarnesses does not synthesize provider-omitted
values, successful Cursor turns cannot yet satisfy the approved cross-provider
usage requirement.

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

- `tth-muse/src/tth_muse/harness/` (Muse Code MSP integration)

- `tth-*/src/tth_*/harness/` usage normalizers for Grok, Cursor, Codex,
  Claude, OpenCode, and Prime Agent
- `tth-cursor/src/tth_cursor/acp/normalizer.py` (Cursor ACP usage mapping)
- `tth-cursor/src/tth_cursor/harness/adapter.py` (terminal response usage)
- `src/talktoharnesses/domain/events.py` (`UsageUpdatedPayload`)
- `tests/live/helpers.py` (shared create/resume usage gate)

## Test evidence

- `tth-muse/tests/test_muse.py`
- `tests/live/test_muse_sandbox_live.py`

- `tth-grok/tests/harness/test_normalizer.py`
- `tth-cursor/tests/acp/test_normalizer.py`
- `tth-codex/tests/harness/test_adapter.py`
- `tth-claude/tests/harness/test_normalizer.py`
- `tth-opencode/tests/harness/`
- `tth-prime-agent/tests/harness/`
- `tests/unit/live/test_helpers.py`
- `tests/live/test_grok_live.py`, `test_cursor_live.py`, `test_codex_live.py`,
  `test_claude_live.py`, `test_opencode_live.py`, and
  `test_prime_agent_live.py`
- Cursor live verification on `2026.08.04-aaa8809`,
  `2026.08.11-e8db854`, and `2026.08.25-3e8eec8` fails at the shared
  pre-terminal `usage_updated` assertion.

## Related

- [Unified harness adapters](../capabilities/unified-harness-adapters.md)
- [Provider adapters](../architecture/provider-adapters.md)
- [Conversation event](../domain/conversation-event.md)
- [Testing guidelines](../operations/testing-guidelines.md)
- [Compatibility and adapters](../maps/compatibility-and-adapters.md)
- [Cursor ACP token-usage limitation](../../raw/engineering/cursor-acp-token-usage-limitation.md)

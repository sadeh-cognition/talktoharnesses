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
last_verified: 2026-09-10
verified_against_commit: aba89791044fd897c152452cccec4d32382058f9
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

A `usage_updated` payload reports the turn's totals so far, never the increment
since the previous payload. A turn may therefore report many times, and each
report supersedes the one before it; that is what lets a consumer show a long
turn's tokens climbing and lets the persisted usage record be replaced rather
than accumulated. Providers whose native events report per-request usage are
accumulated by their adapter so the canonical payload keeps this meaning.

That accumulation is one machine, `tth_types.usage.TurnUsage`, which lives
beside the payload it produces rather than being re-implemented per adapter.
It sums per-request frames (`add`), takes figures that are already the turn's
own and supersedes the running total with them (`replace`), applies one rule
for what counts as a token value (a nonnegative integer), and closes a turn to
frames that trail its terminal figures. A category only some frames reported is
left absent rather than summed into an understated figure, and no category is
derived from another. Each adapter keeps only its own wire-shape mapping.

Grok accumulates the per-response usage its live `_x.ai` `response_completed`
notification carries and is superseded by its `turn_completed` figures. Those
live frames count fresh input apart from cache reads where the terminal frame
counts input cache-inclusive, so the adapter adds cache reads back in and every
report a turn makes stays on one scale. They carry no total, so the running
reports omit that category until the terminal frame supplies it. Codex counts
per thread rather than per turn: the turn's totals are the difference between
the thread's running total and the reading taken before the turn's first
request, which a dropped or replayed notification cannot distort. Claude
accumulates per-assistant-message usage while a turn runs, and its terminal
result supersedes that running total with per-model usage aggregated with a
top-level fallback; the Anthropic per-message usage block carries no total, so
the running reports omit that category too. OpenCode aggregates unique
`step-finish` parts and reconciles message history before terminal. Prime Agent
aggregates assistant-message usage. Muse Code accumulates MSP per-completion
usage for the active turn and is superseded by the aggregate its terminal frame
reports. These six providers produce the existing canonical `usage_updated`
payload before a successful turn terminal.

Because a turn reports many times, `tth.usage_observations` counts reports
rather than turns. Token volume is recorded separately, once per turn, when the
turn's terminal event arrives: `tth.turn_tokens` takes the last total the turn
reported, and `tth.token_cost` carries reported cost alone so the two are not
mixed in one distribution. A provider that reports no total has its input and
output added for that metric only; the canonical payload still omits it.

Grok, Claude, and Codex report while a turn runs; OpenCode, Prime Agent, and
Cursor report only once the turn ends. The ACP `usage_update` branch Grok's
normalizer inherits is not exercised by shipped Grok builds, and any
session-notification kind other than `response_completed` and `turn_completed`
that carries usage is logged rather than mapped, so a further signal is
discovered rather than silently dropped.

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

Two Claude figures are unverified against a live capture. Its terminal
`ResultMessage.usage` is assumed to aggregate the turn, which is what makes the
running per-message sum comparable to it; if that block instead reports a single
request, the running figure would climb past the terminal one. And
`cache_creation_input_tokens` is counted by neither the running reports nor the
terminal ones, so Claude's `input_tokens` is fresh uncached input rather than
billed input. Both are held as they are, consistently across the two paths,
until a live capture settles them.

Headless `stream-json` token totals are not an ACP substitute for the existing
Cursor adapter. Because TalkToHarnesses does not synthesize provider-omitted
values, successful Cursor turns cannot yet satisfy the approved cross-provider
usage requirement.

## Acceptance criteria

- Grok, Cursor, Codex, Claude Code, OpenCode, and Prime Agent emit canonical
  usage for newly executed successful turns.
- Usage is attributed to the active turn and precedes its terminal event.
- Every payload reports the turn's totals so far, so a consumer replaces what
  the same turn reported before it and never adds successive payloads together.
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
- `tth-types/src/tth_types/usage.py` (`TurnUsage`, the shared accumulator)
- `src/talktoharnesses/application/observability.py` (records each turn's tokens
  once, when its terminal event arrives, so repeated reports are not counted
  more than once)
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
- `tth-types/tests/test_usage.py`
- `tests/unit/application/test_observability.py`
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

# Harness token usage requirements

Approved: 2026-08-21

TalkToHarnesses must expose provider-reported token usage through the canonical
`usage_updated` event for successful turns from every supported harness: Grok,
Cursor, Codex, Claude Code, OpenCode, and Prime Agent.

- Usage belongs to the active canonical turn and is emitted before its terminal
  event.
- Token categories remain optional because providers expose different native
  fields. TalkToHarnesses does not synthesize categories the provider omitted.
- Meaningful live evidence contains at least one positive token value, with
  every reported value represented as a nonnegative integer.
- Every provider live gate requires meaningful usage for both its successful
  create and resume turns.
- The requirement applies to newly executed turns. Historical runs are not
  backfilled.
- Adding cost normalization is outside this requirement.

# Cursor ACP token-usage limitation

Snapshot date: 2026-08-30

Cursor Agent does not currently report token usage through its Agent Client
Protocol (ACP) transport. This is an upstream limitation rather than a missing
TalkToHarnesses token mapping.

- Cursor staff confirmed on 2026-05-12 that `PromptResponse.usage` was not
  populated in ACP mode for Cursor Agent `2026.05.09-0afadcc` and recorded it
  as a CLI bug without an estimated fix date:
  https://forum.cursor.com/t/160395
- Cursor staff confirmed on 2026-07-10 that Cursor Agent
  `2026.07.09-a3815c0` did not emit ACP `usage_update`, despite token usage
  being available in the interactive interface, and recorded it as a separate
  feature request without a timeline:
  https://forum.cursor.com/t/165358
- TalkToHarnesses live checks observed the same missing usage on Linux with
  Cursor Agent `2026.08.04-aaa8809`, `2026.08.11-e8db854`, and
  `2026.08.25-3e8eec8`. Agent turns completed, but no canonical
  `usage_updated` event preceded the successful terminal event.

Cursor's February 2026 CLI changelog describes per-turn token totals for
headless `stream-json` output. That is a different transport and does not
establish token reporting in ACP:
https://cursor.com/docs/cli/changelog

TalkToHarnesses must not synthesize the missing values. The Cursor adapter can
normalize ACP usage when Cursor supplies it, while the shared live gate remains
the evidence that the approved cross-provider token-usage requirement is not
yet satisfied for Cursor.

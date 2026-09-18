---
type: map
title: Compatibility and Adapters
status: maintained
audiences:
  - developer
tags:
  - type/map
  - audience/developer
last_verified: 2026-09-18
verified_against_commit: d337f342d5fe7bb427ad5235880c1a1aca09f677
---

# Compatibility and Adapters

Compatibility is a packaged floor plus live probe. Adapters must not claim operations they do not implement.

## Providers

Grok, Cursor, Codex, Claude Code, OpenCode, Prime Agent, and Muse Code each have a
split-owned adapter, packaged floor JSON, and live gate. Every live gate reaches
the split through its on-demand Docker sandbox and requires meaningful
token usage on successful create and resume turns. The Cursor gate currently
fails that requirement because verified Cursor Agent releases omit usage from
ACP. Models, modes, and efforts come from the CLI or SDK installed in that
split.

Muse Code owns its MSP v1 floor in `tth-muse/src/tth_muse/data/compatibility/muse.json`.
Its adapter uses the same HTTP/SSE contract as the existing splits.

Codex brokers MCP tool confirmation elicitations through its existing approval
interactions. See [supported shapes and limitations](../requirements/resolve-approvals-and-structured-questions.md).
Its [adapter](../architecture/provider-adapters.md) preserves native error
messages and retries, with terminal outcomes driven by `turn/completed`.

## Capability flags

Resume, interrupt, steer, multi-interaction, and nested activity are adapter-owned flags. Resume is claimed only when the live agent advertises session loading. `latest_verified` is advisory.

Muse Code `1.0.3-R2198.1` passes the live create/resume usage checks, but
approval delivery after resume can fail with MSP `-32603` and an approval
ledger durability-fence error. The error also reproduces with Meta's SDK
outside TTH and Docker after resuming a completed session. The adapter follows
the SDK's approval routing, decision deduplication, command identities, and
bounded non-admission retries. The live gate checks persisted answer-command
outcomes because interaction counts and turn completion can pass despite failed
delivery. `latest_verified` remains unset. Evidence: `tth-muse/README.md`,
`tth-muse/tests/test_muse.py`, and `tests/live/test_muse_sandbox_live.py`.

## Related

- [Unified harness adapters](../capabilities/unified-harness-adapters.md)
- [Floor-and-probe compatibility](../capabilities/floor-and-probe-compatibility.md)
- [Provider adapters](../architecture/provider-adapters.md)
- [Adapter protocol](../interfaces/adapter-protocol.md)
- [Floor-and-probe compatibility decision](../decisions/floor-and-probe-compatibility.md)
- [Probe and configure harnesses](../requirements/probe-and-configure-harnesses.md)
- [Report harness token usage](../requirements/report-harness-token-usage.md)

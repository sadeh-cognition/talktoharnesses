---
type: capability
title: Unified Harness Adapters
status: implemented
audiences:
  - product
  - developer
tags:
  - type/capability
  - capability/adapters
  - status/implemented
last_verified: 2026-09-05
verified_against_commit: 92bdf81138628204f7b58df5f1f80545abdbbde3
---

# Unified Harness Adapters

TalkToHarnesses drives Grok, Cursor, Codex, Claude Code, OpenCode, Prime Agent, and Muse Code through one adapter protocol. Provider-specific types do not leak into the facade, HTTP API, or domain models.

## Product value

[Target users](../concepts/target-users.md) select a harness kind, working directory, and optional model, mode, and effort. The package accepts identities at or above the packaged floor for the current platform. Models, modes, and efforts come from the live CLI.

Each kind runs in a separate split service. Process-bound splits locate their external CLI on the split's PATH or from its `TALKTOHARNESSES_*_EXECUTABLE` override; Codex and Claude splits pin their SDK dependencies. The proxy never installs, upgrades, or constructs provider command lines.

## Current implementation

Each split implements `HarnessAdapter` with probe, start, resume, submit, steer, interrupt, answer_interaction, events, and close. The proxy registry constructs the same `RemoteHarnessAdapter` for all seven kinds. Cursor model selectors use the string `model` field (`model-id[key=value,...]`). `yolo: true` suppresses approval prompts through provider-native mechanisms. `mcp_servers` attaches streamable HTTP MCP servers on Claude Code, Cursor, Grok, Codex, and Muse Code (the last through a private per-host settings directory); OpenCode and Prime Agent reject the field with `provider_incompatible`, and every split advertises `supports_mcp_servers`.

Adapters map provider-reported token counts into the canonical `usage_updated`
event before successful turn completion. Categories remain absent when the
provider does not report them. Cursor's ACP adapter supports the canonical
mapping, but verified Cursor Agent releases currently omit the native usage
data, so Cursor turns do not emit `usage_updated`.

Muse Code (`kind: "muse"`) discovers models with MSP `model/list`, persists
native sessions, and maps streamed items, usage, approvals, questions, steering,
and interruption into the existing adapter contract. Mode and effort discovery
are unavailable in MSP; those configuration fields are rejected. Nested activity
is not advertised. Evidence: `tth-muse/src/tth_muse/harness/adapter.py`,
`tth-muse/tests/test_muse.py`, and `tests/live/test_muse_sandbox_live.py`.

## Requirements

- [Probe and configure harnesses](../requirements/probe-and-configure-harnesses.md)
- [Steer, interrupt, and switch harness](../requirements/steer-interrupt-and-switch-harness.md)
- [Report harness token usage](../requirements/report-harness-token-usage.md)

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

- [Adapter protocol](../interfaces/adapter-protocol.md)
- [Provider adapters](../architecture/provider-adapters.md)
- [Floor-and-probe compatibility](floor-and-probe-compatibility.md)
- [Compatibility and adapters](../maps/compatibility-and-adapters.md)

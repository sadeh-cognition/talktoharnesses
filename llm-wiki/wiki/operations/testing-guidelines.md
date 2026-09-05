---
type: operation
title: Testing Guidelines
status: maintained
audiences:
  - developer
tags:
  - type/operation
  - audience/developer
last_verified: 2026-09-05
verified_against_commit: 92bdf81138628204f7b58df5f1f80545abdbbde3
---

# Testing Guidelines

The non-live suite covers unit, contract, property, e2e, packaging, and docs checks. Aggregate statement coverage for `talktoharnesses` must be at least 91 percent (migrations omitted). Do not add trivial assertions solely to move coverage.

Live gates are opt-in per provider, prove create/resume/advertised capabilities through the official HTTP client, and run the kind's split in its on-demand Docker sandbox (pre-build the image with deploy/build-splits.sh to keep gates fast). Provider executables and credentials belong to the split container. Enabled gates fail rather than skip when Docker, credentials, or floors are missing. Every provider gate also requires a pre-terminal `usage_updated` event with at least one positive token value for its successful create and resume turns. Do not mix live files into a unit pytest session.

Performance tests measure package-owned database and event-delivery work only.

The Cursor live gate currently fails the token-usage assertion because verified
Cursor Agent releases omit usage from ACP. The gate remains strict so the
upstream limitation stays visible and no provider-omitted values are
synthesized.

Closed-loop coverage of adapter emit → interaction broker →
`answer_interaction` → turn continue, under a running command worker, is
proposed as an in-process orchestration harness. See
[Orchestration Interaction Test Harness](../analyses/orchestration-interaction-test-harness.md).
That suite is not in the tree at the inspected commit.

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

- [Orchestration Interaction Test Harness](../analyses/orchestration-interaction-test-harness.md)
- [Engineering orchestration interaction test harness source](../../raw/engineering/orchestration-interaction-test-harness.md)
- [Engineering live-testing source](../../raw/engineering/live-testing.md)
- [Engineering token-usage live-testing addendum](../../raw/engineering/live-testing-token-usage.md)
- [Performance gates](performance-gates.md)
- [Development guidelines](development-guidelines.md)
- [Floor-and-probe compatibility](../capabilities/floor-and-probe-compatibility.md)
- [Report harness token usage](../requirements/report-harness-token-usage.md)

---
type: operation
title: Testing Guidelines
status: maintained
audiences:
  - developer
tags:
  - type/operation
  - audience/developer
last_verified: 2026-08-30
verified_against_commit: 2920f5820783245bd5b871e61edd44642ebfe56a
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

## Related

- [Orchestration Interaction Test Harness](../analyses/orchestration-interaction-test-harness.md)
- [Engineering orchestration interaction test harness source](../../raw/engineering/orchestration-interaction-test-harness.md)
- [Engineering live-testing source](../../raw/engineering/live-testing.md)
- [Engineering token-usage live-testing addendum](../../raw/engineering/live-testing-token-usage.md)
- [Performance gates](performance-gates.md)
- [Development guidelines](development-guidelines.md)
- [Floor-and-probe compatibility](../capabilities/floor-and-probe-compatibility.md)
- [Report harness token usage](../requirements/report-harness-token-usage.md)

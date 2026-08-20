---
type: operation
title: Testing Guidelines
status: maintained
audiences:
  - developer
tags:
  - type/operation
  - audience/developer
last_verified: 2026-08-20
verified_against_commit: bffc9566181f4309b7f22d446dc950451d78a0d1
---

# Testing Guidelines

The non-live suite covers unit, contract, property, e2e, packaging, and docs checks. Aggregate statement coverage for `talktoharnesses` must be at least 91 percent (migrations omitted). Do not add trivial assertions solely to move coverage.

Live gates are opt-in per provider, prove create/resume/advertised capabilities through the official HTTP client, and fail rather than skip when credentials or floors are missing. Do not mix live files into a unit pytest session.

Performance tests measure package-owned database and event-delivery work only.

Closed-loop coverage of adapter emit → interaction broker → `answer_interaction` → turn continue, under a running command worker, is proposed as an in-process orchestration harness. See [Orchestration Interaction Test Harness](../analyses/orchestration-interaction-test-harness.md). That suite is not in the tree at the inspected commit.

## Related

- [Orchestration Interaction Test Harness](../analyses/orchestration-interaction-test-harness.md)
- [Engineering orchestration interaction test harness source](../../raw/engineering/orchestration-interaction-test-harness.md)
- [Engineering live-testing source](../../raw/engineering/live-testing.md)
- [Performance gates](performance-gates.md)
- [Development guidelines](development-guidelines.md)
- [Floor-and-probe compatibility](../capabilities/floor-and-probe-compatibility.md)

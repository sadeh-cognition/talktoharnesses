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
verified_against_commit: bb3d2b755500fc663816d6cbd1a7cd7947a8920b
---

# Testing Guidelines

The non-live suite covers unit, contract, property, e2e, packaging, and docs checks. Aggregate statement coverage for `talktoharnesses` must be at least 91 percent (migrations omitted). Do not add trivial assertions solely to move coverage.

Live gates are opt-in per provider, prove create/resume/advertised capabilities through the official HTTP client, and fail rather than skip when credentials or floors are missing. Do not mix live files into a unit pytest session.

Performance tests measure package-owned database and event-delivery work only.

## Related

- [Engineering live-testing source](../../raw/engineering/live-testing.md)
- [Performance gates](performance-gates.md)
- [Development guidelines](development-guidelines.md)
- [Floor-and-probe compatibility](../capabilities/floor-and-probe-compatibility.md)

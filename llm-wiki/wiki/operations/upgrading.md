---
type: operation
title: Upgrading
status: maintained
audiences:
  - developer
tags:
  - type/operation
  - audience/developer
last_verified: 2026-08-20
verified_against_commit: bb3d2b755500fc663816d6cbd1a7cd7947a8920b
---

# Upgrading

Conservative stop/migrate/start is required. Mixed-version rolling upgrades are not supported. Backward migration compatibility is not promised.

The current release resets migration history and supports only new databases. Stored harness configurations that include unknown fields fail validation and must be recreated. Read generated `SUPPORTED_HARNESSES.md` and release notes before changing the package.

## Related

- [Engineering upgrading source](../../raw/engineering/upgrading.md)
- [Deployment](deployment.md)
- [Releasing](releasing.md)
- [Floor-and-probe compatibility](../capabilities/floor-and-probe-compatibility.md)

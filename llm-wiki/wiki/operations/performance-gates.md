---
type: operation
title: Performance Gates
status: maintained
audiences:
  - developer
tags:
  - type/operation
  - audience/developer
last_verified: 2026-08-20
verified_against_commit: bb3d2b755500fc663816d6cbd1a7cd7947a8920b
---

# Performance Gates

Release performance gates measure package-owned database and event-delivery work only. They are regression budgets for the documented CI reference profile, not latency SLAs for arbitrary hardware or provider networks.

Do not benchmark provider response time, network latency, model generation, package installation, or process startup. Query-count increases fail even when wall-clock budgets pass.

## Related

- [Engineering performance source](../../raw/engineering/performance.md)
- [Testing guidelines](testing-guidelines.md)
- [Persistence and event sequencing](../architecture/persistence-and-event-sequencing.md)

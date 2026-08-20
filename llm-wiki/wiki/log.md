---
type: log
title: Wiki Log
status: maintained
audiences:
  - product
  - developer
tags:
  - type/log
last_verified: 2026-08-20
verified_against_commit: bffc9566181f4309b7f22d446dc950451d78a0d1
---

# Wiki Log

Entries are appended using `## [YYYY-MM-DD] operation | Title`.

## [2026-08-20] operation | Propose orchestration interaction test harness

- Added engineering source `raw/engineering/orchestration-interaction-test-harness.md`.
- Added proposed analysis [Orchestration Interaction Test Harness](analyses/orchestration-interaction-test-harness.md).
- Recorded a test-evidence gap on approval requirements and testing guidelines.

## [2026-08-20] operation | Remove product-name mentions from the vault

- Replaced named-product examples with generic remote HTTP clients, wiki web
  viewers, and the `wiki_lint` command.

## [2026-08-20] operation | Move lint handoff rule to development guidelines

- Moved the `make lint` handoff sentence from repository `AGENTS.md` into
  [Development guidelines](operations/development-guidelines.md).
- `AGENTS.md` now routes agents to the wiki instead of restating that rule.

## [2026-08-20] operation | Create TalkToHarnesses LLM wiki

- Added an Obsidian vault at `llm-wiki/`.
- Snapshotted README, accepted ADRs, and operator docs under `raw/`.
- Authored maps, capabilities, requirements, journeys, architecture, interfaces, domain, decisions, and operations pages for the public contract.
- Verified against TalkToHarnesses baseline
  `bb3d2b755500fc663816d6cbd1a7cd7947a8920b` plus uncommitted floor-and-probe
  compatibility work present in the working tree.

## Related

- [Wiki index](index.md)
- [Wiki maintenance](operations/wiki-maintenance.md)

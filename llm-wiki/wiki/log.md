---
type: log
title: Wiki Log
status: maintained
audiences:
  - product
  - developer
tags:
  - type/log
last_verified: 2026-08-21
verified_against_commit: 3f90f85a37028a1ba0498cff641ef5c8a1bec6d7
---

# Wiki Log

Entries are appended using `## [YYYY-MM-DD] operation | Title`.

## [2026-08-21] implement | Locate process-bound CLIs from kind

- Removed `executable_path` from harness create/configuration contracts.
  Grok, Cursor, OpenCode, and Prime Agent binaries are located at probe and
  launch from PATH or `TALKTOHARNESSES_*_EXECUTABLE`. Codex and Claude stay
  SDK-bundled. New and stored configuration containing `executable_path` is
  rejected and must be recreated.
- Preserved the approved TTH-owned executable-discovery source, which
  supersedes the older statement that TTH never discovers external CLIs.
- Updated README create examples and derived probe, harness-instance, glossary,
  adapter, overview, and upgrading pages.
- Verified against TalkToHarnesses baseline
  `3f90f85a37028a1ba0498cff641ef5c8a1bec6d7` plus the uncommitted
  implementation recorded with this entry.

## [2026-08-21] operation | Record TTH abbreviation

- Added product source `raw/product/abbreviation.md`.
- Documented TTH as the abbreviation for TalkToHarnesses in the
  [glossary](glossary.md), [overview](overview.md),
  [product overview](maps/product-overview.md), and vault home.

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

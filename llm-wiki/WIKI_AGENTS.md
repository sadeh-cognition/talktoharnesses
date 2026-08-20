# TalkToHarnesses Wiki Maintainer Contract

This file defines how an LLM maintains the Obsidian vault rooted at `llm-wiki/`.

## Authority

- Human-approved material in `raw/product/` and accepted ADRs in `raw/engineering/`
  describe intended behavior.
- The repository at a recorded commit, including tests and schemas, describes
  implemented behavior. Operator docs under repository `docs/` are snapshotted
  into `raw/engineering/` rather than linked from managed pages.
- `wiki/` is a derived synthesis. Never treat an unsupported wiki statement as
  primary evidence.
- Never silently reconcile conflicting sources. Record the intended behavior,
  current behavior, and gap separately.
- Never edit material under `raw/` during ingestion. Add a new source or ask a
  human to amend it.

## Page contract

Generated pages use kebab-case filenames, one H1 matching the `title` property,
and YAML properties containing at least:

```yaml
---
type: requirement
title: Example title
status: implemented
audiences:
  - product
  - developer
tags:
  - type/requirement
last_verified: YYYY-MM-DD
---
```

Use standard relative Markdown links with explicit `.md` extensions. Do not
require Wikilinks, Dataview, or another Obsidian plugin. Put meaningful
relationships in prose and in a final `Related` section. Tags are filters, not
substitutes for links.

Allowed page types are `map`, `capability`, `requirement`, `journey`, `domain`,
`architecture`, `interface`, `decision`, `operation`, `analysis`, `source`,
`entity`, `concept`, `overview`, `index`, `log`, and `glossary`.

Use controlled status values where they apply: `proposed`,
`partially-implemented`, `implemented`, `deprecated`, `maintained`,
`historical`, and `source`.

Use namespaced tags such as `type/requirement`, `audience/product`,
`capability/adapters`, `status/implemented`, and `review/stale`.

Requirements must contain these headings:

- `Intent`
- `Current behavior`
- `Gap`
- `Acceptance criteria`
- `Implementation evidence`
- `Test evidence`
- `Related`

When a page makes claims about implemented behavior, add
`verified_against_commit` and keep code and test paths in the evidence sections.
A commit identifies the inspected baseline; it does not imply that later
uncommitted changes were absent.

Do not link to files outside this vault. Cite repository paths as prose.

## Link and attachment rules

- The vault root is `llm-wiki/`; `Home.md` is its Obsidian entry point.
  Agentbahn opens `wiki/index.md` first.
- Prefer links to pages over links to headings when the concept has its own page.
- Do not create generic links solely to silence orphan checks.
- Each substantive page must have an inbound link from a map, capability, or journey.
- Store derived media in `wiki/assets/` and immutable source attachments in `raw/assets/`.
- Use relative embeds for images so both Obsidian and the Agentbahn web viewer can resolve them.
- When moving or renaming pages, update every inbound Markdown link in the same change.

## Ingest workflow

1. Identify the source type: approved product input, engineering proposal,
   external material, or repository change.
2. Preserve new documentary sources under the matching `raw/` directory. For
   repository evidence, record the inspected commit instead of copying source files.
3. Read `Home.md`, `wiki/index.md`, and the relevant Maps of Content before
   selecting pages to update.
4. Update existing pages when the information changes an existing concept.
   Create a page only for a distinct concept that other pages need to link to.
5. Update all affected capability, requirement, journey, interface, and
   decision pages in the same ingest.
6. Record contradictions as gaps or unresolved questions. Product intent and
   acceptance criteria require human approval.
7. Update the relevant maps and `wiki/index.md`.
8. Append one entry to `wiki/log.md` using `## [YYYY-MM-DD] operation | Title`.
9. Run Agentbahn `wiki_lint --root` against this vault and resolve errors before
   completing the ingest.

## Query workflow

1. Route from `wiki/index.md` or a relevant map instead of scanning every page.
2. Verify important current-behavior claims against primary repository evidence.
3. Cite the wiki pages and primary evidence used in the answer.
4. Save a result under `wiki/analyses/` only when it is reusable beyond the
   immediate conversation.
5. Link a saved analysis from the relevant maps and pages, then update the log
   and run the linter.

## Lint and review workflow

Run Agentbahn's wiki linter after every maintenance pass:

```bash
uv run manage.py wiki_lint --root /path/to/talktoharnesses/llm-wiki
```

The deterministic linter checks metadata, titles, links, duplicate titles,
requirement sections, and orphan pages. A semantic review must additionally inspect:

- stale claims after code or requirement changes;
- contradictions across pages;
- requirements without credible implementation or test evidence;
- product intent or acceptance criteria inferred without approval;
- overly broad pages that should be split, and tiny duplicate pages that should be merged.

Human approval is required before changing product intent, acceptance criteria,
architectural decisions, or the resolution of a conflict.

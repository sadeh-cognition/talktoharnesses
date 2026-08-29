---
type: operation
title: Wiki Maintenance
status: maintained
audiences:
  - developer
  - product
tags:
  - type/operation
  - audience/developer
last_verified: 2026-08-30
verified_against_commit: 78003994d9fe93108ce5a6bc3591ab2e2ef904d9
---

# Wiki Maintenance

The wiki is a derived, version-controlled Obsidian vault. `WIKI_AGENTS.md` is the authoritative maintenance contract.

## Open the vault

Open the repository's `llm-wiki/` directory as an Obsidian vault. Shared settings configure relative Markdown links, automatic link updates, and `wiki/assets/` as the default attachment directory. Personal workspace state is ignored by Git.

## Add knowledge

1. Classify a new documentary source under `raw/product/` or `raw/engineering/` without modifying older sources.
2. For code evidence, inspect the repository and record the commit instead of copying code into `raw/`.
3. Update affected capability, requirement, journey, and technical pages.
4. Update Maps of Content and the index.
5. Append the operation to the log.
6. Run the wiki linter:

```bash
uv run host/manage.py wiki_lint --root /path/to/talktoharnesses/llm-wiki
```

The command is provided by the `talktoharnesses.django` application and uses
only filesystem access; it does not read or modify application data.

## Related

- [TalkToHarnesses Knowledge Base](../../Home.md)
- [Product overview](../maps/product-overview.md)
- [Developer overview](../maps/developer-overview.md)
- [Wiki log](../log.md)

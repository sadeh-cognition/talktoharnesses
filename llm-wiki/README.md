# TalkToHarnesses LLM Wiki

This directory is an Obsidian-first knowledge base connecting TalkToHarnesses
public contracts to architecture, implementation, and tests.

Open `llm-wiki/` as an Obsidian vault and begin at [Home](Home.md). The content
uses portable standard Markdown and can be read through Agentbahn's project wiki
UI, which serves `<project>/llm-wiki`.

Human operator docs in the repository `docs/` directory remain canonical for
install, deploy, upgrade, and release procedures. This vault snapshots those
sources under `raw/` and synthesizes navigable pages under `wiki/`.

## Structure

- `raw/` contains immutable product and engineering source material.
- `wiki/maps/` contains curated Maps of Content.
- `wiki/capabilities/`, `wiki/requirements/`, and `wiki/journeys/` connect intent to delivered behavior.
- `wiki/architecture/`, `wiki/interfaces/`, `wiki/domain/`, `wiki/decisions/`, and `wiki/operations/` document engineering knowledge.
- `wiki/index.md` catalogs the maintained content.
- `wiki/log.md` is the append-only maintenance history.

## Operating rules

Approved sources under `raw/` describe intended behavior. The repository at a
recorded commit, including tests, describes current behavior. The generated wiki
must show disagreements explicitly.

Follow [WIKI_AGENTS.md](WIKI_AGENTS.md) when maintaining the vault. Validate
with Agentbahn's linter pointed at this root:

```bash
# from the Agentbahn repository
uv run manage.py wiki_lint --root /path/to/talktoharnesses/llm-wiki
```

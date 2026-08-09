# Phase 11 — Search, Retention, and Transcript Portability

## Summary

- Start Phase 11 only from the released `2026.8.0` result produced by [Phase 12](phase12.md) and
  keep that release immutable. Develop this increment as `2026.8.1.dev1`; change it to `2026.8.1`
  only after the complete Phase 11 gate and the Phase 12 publication gates pass again.
- Extend the existing Phase 8 search product with deterministic relevance ordering, bounded plain
  text snippets, and a deliberately small query language. Keep PostgreSQL full-text search and
  SQLite FTS5 as the only backends.
- Replace the fixed six-calendar-month retention rule with one owner-scoped month count, retain six
  months as the default, and add per-conversation history exemptions plus read-only previews. Keep
  cleanup externally scheduled and reuse the existing prune/session-rotation path.
- Add a versioned, provider-neutral canonical transcript document that can be exported from retained
  history or imported into a new conversation. Reuse the Phase 8 handoff representation and
  candidate-runtime seeding path internally.
- Keep one conversation shell, one canonical event stream, one binding model, one runtime manager,
  and the existing five fixed adapters. Phase 11 does not add a plugin system, generic runtime
  strategies, provider-native import, native-session exposure, or `HarnessAdapter` changes.
- Do not add fuzzy search, stemming/language configuration, autocomplete, saved searches, an
  external search service, an in-process scheduler, workspace cleanup, provider-side deletion,
  approval-audit deletion, LLM summaries, transcript attachments, or reasoning/raw-event transfer.

## Current Baseline and Entry Conditions

Phase 11 begins after Phase 12 has released and verified `2026.8.0`. Before implementation, rerun
the built-wheel definition-of-done journeys and confirm that the release checkout has no migration,
compatibility-document, generated-document, or public-export drift. Product work must not be
developed against a partially completed Phase 9, Phase 10, or Phase 12 branch.

The roadmap's Phase 11 bullets identify the phase that would own several future product ideas; they
are not a requirement to implement all of them together. This plan selects the smallest coherent
search, retention, and transcript increment and records the remaining ideas as out of scope.

The Phase 10 baseline provides the required seams:

- `SearchDocument.normalized_text` is built from retained, redacted title/message/tool knowledge by
  `application.search_documents`. PostgreSQL derives a `tsvector`/GIN index and SQLite derives an
  FTS5 table from that one column. Search currently applies implicit AND to normalized terms and
  orders matches by conversation update time.
- `ConversationShell` is the single list/search shell. The facade and `/api/v1/conversations/search`
  return `Page[ConversationShell]` with opaque keyset cursors.
- Retention uses the externally invoked `talktoharnesses_cleanup` command, one fixed six-month UTC
  cutoff, short per-conversation prune transactions, and the Phase 8 candidate-runtime path to
  rotate native context after history deletion.
- Pruning already deletes complete turn-owned aggregates, cancels expired waiting interactions,
  leaves approval audits and workspace files alone, recomputes derived title/search projections,
  preserves event sequence high-water, and rejects stale runtimes through binding/version checks.
- `HandoffDocument` is the one ordered representation of retained user/assistant messages and
  canonical tool results. It excludes reasoning, plans, raw/native events, stderr, full tool output,
  and deleted turns. Switching, retention rotation, and Phase 9 recovery all use its deterministic
  renderer and the same candidate-runtime lifecycle.
- The API and facade already share strict Pydantic projections, owner scoping, generic not-found
  behavior, opaque cursors, committed SSE publication, and the stable five-adapter registry.

Fix any regression in those owning phases before beginning Phase 11. Do not compensate for a
missing Phase 8 retention or handoff invariant with an import-specific data path.

## Phase 11 Invariants

1. Canonical retained rows remain authoritative. Search text, snippets, retention previews, and
   transcript documents are derived views and never become a second editable history store.
2. Every search candidate is owner-scoped and excludes soft-deleted conversations before ranking
   or snippet construction. Search syntax cannot inject backend-specific SQL, FTS, or query syntax.
3. PostgreSQL and SQLite implement the same query grammar, match set, relevance formula, tie-breaks,
   and cursor semantics. Backend-native FTS is an index, not a product-specific ranking contract.
4. Search snippets contain only text already allowed into the sanitized search document. They never
   read reasoning, raw/native events, stderr, secrets, full tool output, or workspace files.
5. Six calendar months remains the effective retention period until an owner explicitly changes it.
   Retention is expressed in calendar months in UTC, never converted to a fixed number of days.
6. A retention exemption protects turn history for one live conversation. It does not resurrect
   deleted rows, preserve a soft-deleted conversation forever, or exempt approval audits from their
   existing indefinite retention.
7. A dry-run or preview is read-only: it does not cancel interactions, delete rows, allocate event
   sequences, publish events, start/close runtimes, rotate bindings, or touch provider sessions.
8. Changing a policy affects future cleanup passes only. It never performs cleanup in an HTTP
   request or rewrites timestamps to simulate a different age.
9. Transcript export includes only the same retained canonical knowledge allowed in
   `HandoffDocument`. Export after pruning cannot recover deleted content from events or native
   sessions.
10. Transcript import creates a new owner-scoped conversation and a new native session. It never
    attaches imported content to an existing conversation, reuses source IDs, resumes a source
    native session, or exposes native identifiers.
11. Imported history is accepted durably only after a transient candidate session accepts the
    deterministic handoff. Failure leaves no conversation, binding, canonical history, or SSE
    event committed.
12. Search, retention, import, and export add no adapter-specific branches. They use the existing
    provider-neutral persistence, handoff, registry, and runtime contracts.

## Public Contracts

### Search query and result

Keep `GET /api/v1/conversations/search` and
`TalkToHarnessesService.search_conversations(...)`. Change their successful result to
`Page[ConversationSearchHit]` and keep `q`, `cursor`, and `limit` as the only request parameters.

The public result models are:

```python
class SearchSnippet(BaseModel):
    model_config = FROZEN

    text: str
    matched_terms: tuple[str, ...] = ()


class ConversationSearchHit(BaseModel):
    model_config = FROZEN

    conversation: ConversationShell
    snippet: SearchSnippet | None = None
```

Do not expose a database rank or promise that scores are comparable across queries. Ordering is the
contract; a numeric score would create a second public compatibility surface without a caller need.

Parse `q` once in a new pure `application.search_query` module. Support only:

- unquoted positive terms;
- double-quoted positive phrases;
- `-term` and `-"quoted phrase"` exclusions;
- `is:pinned`, `is:archived`, and `has:interaction` filters;
- `harness:grok|cursor|codex|claude|opencode`; and
- `before:YYYY-MM-DD` and `after:YYYY-MM-DD`, applied to `updated_at` with UTC day boundaries.

Terms are implicit AND. Filters may be repeated only when they do not conflict. Require at least one
positive term or phrase so search does not become a second list endpoint. Reject unknown operators,
unclosed quotes, empty phrases, invalid dates/kinds, conflicting filters, more than eight text
clauses, or a query longer than 512 Unicode code points with `invalid_search_query` and HTTP 400.
Do not add `OR`, parentheses, wildcards, field selectors, escapes beyond `\"` and `\\`, or backend
query syntax.

`before` is exclusive at 00:00 UTC on the supplied date; `after` is inclusive at 00:00 UTC. Dates
therefore remain stable across the host's local time zone.

Rank matches by one fixed integer formula calculated over the stored Python-normalized title and
body fields:

- 32 points for each positive clause occurring in the complete normalized title;
- 8 points for each positive term occurrence in the title, capped at four occurrences per term;
- 4 points for each positive phrase occurrence in the retained body, capped at four; and
- 1 point for each positive term occurrence in the retained body, capped at eight.

For ranking, “term” includes each normalized token inside a phrase. Count only complete normalized
tokens or complete normalized token sequences, not substrings inside a token. Both SQL compilers use
the same space-delimited occurrence definition and integer caps.

Order by rank descending, then `updated_at` descending, then conversation UUID descending. The
opaque cursor carries all three values and a query digest; reject a cursor reused with a different
normalized query. Compile the same formula to bounded backend-specific SQL expressions in the
Django persistence implementation. Keep the formula and caps as constants in
`application.search_query`; SQL compilation consumes the parsed query and constants rather than
redeclaring product rules.

Construct at most one 240-code-point plain-text snippet around the first positive clause in the
stored sanitized body. `matched_terms` contains the distinct query terms actually present in that
snippet, in query order. Clients decide how to render highlighting; the server returns no HTML and
therefore creates no markup-escaping contract. Return `snippet=null` when the query matches only the
title.

### Retention policy and exemption

Add these public projections:

```python
class RetentionPolicyProjection(BaseModel):
    model_config = FROZEN

    months: int = Field(ge=1, le=120)
    updated_at: UtcDateTime | None = None


class RetentionPreviewProjection(BaseModel):
    model_config = FROZEN

    cutoff: UtcDateTime
    soft_deleted_conversations: int
    history_conversations: int
    terminal_turns: int
    waiting_turns: int
```

Expose facade methods and authenticated routes with the same projections:

- `get_retention_policy(owner_id)` / `GET /api/v1/retention`;
- `replace_retention_policy(owner_id, months)` / `PUT /api/v1/retention`;
- `preview_retention(owner_id)` / `GET /api/v1/retention/preview`; and
- `set_retention_exemption(owner_id, conversation_id, exempt)` /
  `PUT /api/v1/conversations/{id}/retention-exemption`.

The replacement request contains only `months`. The exemption request contains only `exempt`.
Expose `retention_exempt` on the existing `Conversation` and therefore its detail/snapshot; do not
add it to `ConversationShell` or create another conversation projection.

An owner with no stored policy receives `months=6` and `updated_at=null`. Replacing it performs an
upsert. There is no delete/reset endpoint because `PUT {"months": 6}` already restores the default
behavior without another contract.

The preview reports current eligible counts for that owner using one captured database-consistent
`now`. It excludes exempt conversations and running/background-active work exactly as cleanup does.
It reports rows, not estimated bytes, native sessions, paths, prompts, or provider details.

Keep `talktoharnesses_cleanup` as the only execution entry point. Add `--dry-run`, which invokes the
same preview operation across owners and prints aggregate fixed-field counts without mutation. A
normal pass resolves each owner's effective policy and processes existing short transactions; it
does not hold one transaction across owners or provider session rotation.

### Canonical transcript document

Add a public `talktoharnesses.domain.transcripts` module and export its document types through
`talktoharnesses.domain`. The strict JSON shape is provider-neutral:

```python
class TranscriptMessage(BaseModel):
    model_config = FROZEN

    type: Literal["message"] = "message"
    role: Literal["user", "assistant"]
    text: str
    interrupted: bool = False


class TranscriptTool(BaseModel):
    model_config = FROZEN

    type: Literal["tool"] = "tool"
    tool_name: str
    arguments: dict[str, JsonValue] = Field(default_factory=dict)
    outcome: ToolOutcome
    exit_status: int | None = None
    paths: tuple[str, ...] = ()
    output_tail: str = ""


class TranscriptTurn(BaseModel):
    model_config = FROZEN

    entries: tuple[TranscriptMessage | TranscriptTool, ...]


class TranscriptDocument(BaseModel):
    model_config = FROZEN

    format: Literal["talktoharnesses.canonical-transcript"]
    version: Literal[1]
    title: str
    turns: tuple[TranscriptTurn, ...]
```

Use a discriminated union for entries and `extra="forbid"` through the existing frozen model
configuration. Each turn has exactly one first user message and at least one entry. Assistant
messages and tools retain their canonical relative order. Reject system messages, empty turns,
non-JSON tool arguments, strings over their existing canonical field limits, more than 5,000 total
entries, or a canonical JSON representation over 5 MiB.

The format deliberately omits conversation/turn/message/tool UUIDs, owner IDs, harness kind,
bindings, harness configuration, native session IDs, launch snapshots, timestamps, commands,
interactions, rules/audits, activities, plans, reasoning, usage/cost, raw events, stderr, full tool
output, and workspace files. Import allocates all local IDs and timestamps.

Expose:

- `export_transcript(owner_id, conversation_id)` and
  `GET /api/v1/conversations/{id}/transcript`; and
- `import_transcript(owner_id, harness_id, document)` and
  `POST /api/v1/conversations/import`, returning 201 with the new `ConversationSnapshot`.

Export is deterministic for an unchanged retained conversation: use the document's canonical JSON
serialization, stable entry ordering, and one trailing newline in the documented file form. The
HTTP response remains JSON and does not add content-disposition or archive handling.

Import validates and redacts the document before provider work, converts it once to the existing
internal `HandoffDocument`, starts one transient candidate through `RuntimeManager.start_candidate`,
and seeds it through `seed_candidate`. After the candidate terminates successfully, commit the new
conversation, active binding, imported turn/message/tool rows, search document, one
`transcript_imported` canonical event containing only counts, and the accepted launch/session state
in one transaction. Preserve the document title as the new conversation's manual title. Publish
that committed event, promote the candidate, and return the ordinary snapshot. If candidate
start/seed or the database commit fails, close it and commit nothing.

Imported entries are canonical retained history but are not fabricated native streaming events.
The import transaction materializes their rows directly and emits one bounded notification event;
SSE consumers obtain the imported history from the resulting snapshot. Subsequent switching,
retention, search, export, recovery handoff, and turn submission use the ordinary existing readers.

## Work Package 1 — Migration and Shared Derived Data

Create one Phase 11 migration after the Phase 9 schema. Phase 10 has no migrations.

- Add `search_title`, `search_body`, and `snippet_text` to `SearchDocument`. All are sanitized
  derived columns: `search_title` contains the normalized effective title used for title weighting,
  `search_body` contains normalized retained messages/tools without the title, and `snippet_text`
  contains retained canonical display text suitable for a plain-text snippet. `normalized_text`
  remains the indexed complete normalized document.
- Extend `application.search_documents` so one builder returns all four fields. Remove the old
  independently shaped helper return values and make materialization, migration backfill, search,
  title recomputation, pruning, and transcript import consume the same builder.
- Add private `RetentionPolicyRecord(owner_id primary key, months, updated_at)` with a database
  check constraint for 1 through 120. Do not pre-create a row for every owner; absence means six.
- Add `retention_exempt` to the domain `Conversation`, aggregate JSON, and
  `ConversationAggregate`, defaulting to false. Backfill both representations consistently; do not
  add another conversation projection or a policy copy per conversation.
- Add the bounded `transcript_imported` event payload to the strict canonical event union with only
  `turn_count`, `message_count`, and `tool_count`.
- Add `retention_exemption_changed` with only the new boolean value so the existing metadata
  mutation path can commit and publish the conversation-visible change atomically.

The migration recomputes all search derived columns from retained projection rows using a migration
copy of the builder required by Django migration stability. That copy is the historical migration
implementation, not a second live product rule. Verify forward migration on populated SQLite and
PostgreSQL databases and keep reverse migration limited to removing Phase 11 fields/rows; reverse
does not reconstruct transcripts already pruned under a shorter policy.

## Work Package 2 — Ranked Search

1. Implement strict query tokenization/parsing as pure typed code with no Django or provider import.
2. Produce one normalized `SearchQuery` containing positive clauses, exclusions, filters, and a
   stable digest. Query normalization uses the same term normalizer as document construction.
3. Compile candidates separately for PostgreSQL and SQLite while keeping all product decisions in
   the parsed query. PostgreSQL uses parameterized `websearch_to_tsquery`/`phraseto_tsquery`-style
   primitives as appropriate. SQLite quotes FTS5 tokens/phrases with one tested escaping helper and
   passes the resulting expression as a SQL parameter. Never interpolate user text into SQL.
4. Apply metadata filters and owner/deletion predicates before ranking. Exclusions remove a whole
   conversation when its indexed document matches the excluded term or phrase.
5. Compile the fixed integer relevance expression over `search_title` and `search_body`, then
   apply the common rank/update/UUID keyset cursor.
6. Build the bounded snippet from `snippet_text` after fetching only the selected page. Do not issue
   one query per hit.
7. Update the facade, Django route schema, public exports, OpenAPI contract, and performance
   fixture.
8. Rebuild/search-document operations remain synchronous transaction work behind the existing
   thread-sensitive async persistence boundary; do not add an indexing worker or cache.

Search tests cover parser limits and escaping, every operator/filter, Unicode normalization,
phrases spanning punctuation, exclusion behavior, invalid cursors/query digests, deterministic
ties, snippet bounds, redaction, owner/deletion isolation, and identical ordered pages on SQLite and
PostgreSQL. Extend the Phase 10 10,000-document search budget to the ranked result path without
loosening its p95 limit or adding an N+1 query.

## Work Package 3 — Configurable Retention

1. Replace `six_months_before(now)` with `months_before(now, months)` as the one calendar helper.
   Preserve month-end/leap-year clamping and have the default-policy path pass six.
2. Add coarse persistence operations for effective-policy read/upsert, owner-scoped preview,
   cleanup candidate enumeration with effective cutoffs, and exemption mutation. Do not expose
   generic retention-policy CRUD.
3. Make preview and cleanup share one pure eligibility classifier over conversation/turn state.
   Persistence supplies counts and locked rows; it does not reimplement eligibility in the route or
   management command.
4. Keep pruning atomic per conversation and keep provider candidate creation outside the database
   transaction. Continue to publish committed rotation events before attempting external rotation.
5. An exemption skips history pruning and native rotation. Soft-deleted rows still purge after the
   owner's configured period, regardless of exemption, so a user deletion is eventually final.
6. A policy shortened below existing history takes effect on the next external cleanup pass. A
   policy lengthened after rows were deleted cannot restore them.
7. Add `--dry-run` to the existing command; do not add another management command or an HTTP
   endpoint that executes cleanup.

Test 1/6/120-month policies, absent-policy default, month ends and leap years, exact cutoffs,
exemption toggles, soft-delete behavior, owner isolation, preview/real-count agreement, dry-run
zero-mutation behavior, waiting/running/background-active states, rerun idempotence, native session
rotation, stale-runtime rejection, and concurrent policy/exemption changes. Run the same behavior
suite against SQLite and PostgreSQL.

## Work Package 4 — Transcript Export and Import

1. Implement strict document models and deterministic JSON dump/load helpers in
   `domain.transcripts`; keep them Django- and provider-free.
2. Add one converter from ordered `HandoffDocument` entries to grouped `TranscriptDocument` turns
   and one reverse converter that allocates prospective local IDs. Do not add a second handoff
   renderer or read canonical rows independently in the service.
3. Export by calling the existing owner-scoped retained-handoff read. Read the current effective
   title in the same transaction so export cannot combine history and title from different
   committed versions.
4. Validate size/count/shape and run the existing redaction boundary before starting a candidate.
   The import document is user input and receives no trust because it came from a prior export.
5. Start and seed a transient candidate with the target owner-scoped harness configuration. Reject
   a candidate interaction or non-successful terminal exactly as switching does.
6. Add one coarse `commit_transcript_import(...)` persistence operation. It creates the
   conversation, binding history/active binding, canonical imported rows, aggregate/search state,
   and bounded event atomically. It never calls a provider while holding a transaction.
7. Close an uncommitted candidate on every failure. After commit, publish once and promote it using
   the existing runtime manager. Do not attempt provider-side deletion of an abandoned native
   session after a process crash.
8. Document that imports execute the transcript as a handoff prompt in the selected local harness
   under the authenticated user's normal workspace permissions. Import is not a sandbox or a
   trusted backup restore.

Test deterministic export, dump/load round trips, unknown fields/versions, limits, malformed tool
JSON, retained-only knowledge, no native/reasoning/raw/secret leakage, cross-owner access, new-ID
allocation, candidate rejection/interaction/timeout, database failure after seeding, no partial
commit, search of imported history, re-export equivalence, retention of imported turns, switch and
recovery handoff, SSE snapshot behavior, and a subsequent ordinary turn on the imported native
session. Run provider-neutral contract tests with fakes; existing live compatibility matrices do
not claim transcript import support and need no new provider capability.

## Work Package 5 — Documentation, Compatibility, and Release Gate

Update README and the deployment/upgrade documents with links to a focused
`docs/search-retention-transcripts.md` guide covering:

- the exact query grammar, ordering, snippet behavior, and invalid-query response;
- owner retention policies, the six-month default, exemptions, previews, dry-run, and external
  scheduling;
- the transcript v1 schema, retained-only exclusions, import limits, and candidate-seeding security
  boundary; and
- the fact that none of these features exposes or deletes provider-native sessions or workspace
  files.

Update the reviewed public-export contract for the new projections/document helpers only. Core
imports remain Django-free. The migration and new modules must be present in wheel/sdist content and
pass the existing isolated core, Django-only, `all`, and sdist installs.

Before changing the development version to `2026.8.1`:

1. Run Ruff, format check, strict Pyright, migration drift, lock check, compatibility/document
   drift, 91% coverage, the Phase 10 performance gates, and the full non-live suite.
2. Run search and retention parity suites on SQLite and PostgreSQL and the transcript import runtime
   suite on Linux, macOS, and Windows where process supervision differs.
3. Build wheel/sdist once and run both definition-of-done journeys from the wheel, adding ranked
   search, retention preview/exemption, transcript export, and import into a second harness.
4. Rerun all five exact live create/resume/interaction gates for the candidate package revision.
   Product-only changes do not permit carrying compatibility evidence from different bytes.
5. Update the five compatibility adapter versions and package metadata to `2026.8.1`, regenerate
   `SUPPORTED_HARNESSES.md`, run the stable compatibility gate, tag, and publish only the verified
   artifacts through the existing Phase 10 workflow.

The Phase 11 gate passes when both databases return the same ranked pages for the defined grammar,
dry-run and preview exactly predict retention eligibility without mutation, an exemption prevents
history pruning, and a retained canonical transcript can move into a fresh provider-neutral native
session without leaking excluded data or creating partial durable state.

## Implementation Order

1. Merge and verify the `2026.8.0` release checkout; set `2026.8.1.dev1` and matching development
   compatibility metadata without changing published matrix rows.
2. Add the Phase 11 migration, unified search-document result, retention policy/exemption fields,
   and strict transcript/event models.
3. Implement the pure search parser/ranking contract, then PostgreSQL/SQLite query compilation,
   cursor, snippet, facade, and API changes.
4. Generalize the calendar cutoff, add policy/exemption/preview persistence, and adapt the existing
   cleanup command and rotation orchestration.
5. Add transcript converters and export, then candidate-seeded import and its single atomic
   persistence commit.
6. Run migration, parity, failure-injection, security, performance, and end-to-end tests; update the
   focused product documentation and public-export/artifact checks.
7. Rerun live compatibility evidence and the full Phase 10 release gate before the stable version
   transition and immutable artifact publication.

## Explicitly Out of Scope

- Fuzzy/edit-distance search, stemming, per-language analyzers/indexes, autocomplete/prefix search,
  saved searches, semantic/vector search, an external search service, search caches, or background
  indexing workers.
- Boolean `OR`, parentheses, arbitrary field syntax, regex, raw SQL/FTS syntax, configurable rank
  weights, configurable snippet length, or a second conversation-list/search projection.
- An in-process or distributed cleanup scheduler, retention execution over HTTP, workspace/file
  cleanup, provider-side session deletion, approval-rule/audit deletion, legal-hold workflows,
  storage quotas, or per-harness retention settings.
- Native-session IDs in any public model, same-provider thread fork/clone endpoints, provider-native
  history discovery/import/export, native transcript fidelity, or adoption of a source provider
  session.
- LLM-generated summaries, transcript attachments/files, plans, activities, reasoning, raw/native
  events, stderr, full tool output, interaction history, approvals, usage/cost, or launch metadata
  in transcript v1.
- Dynamic plugins/provider discovery, adapter-specific binding tables, generic runtime strategies,
  new providers, additional provider capabilities, or changes to `HarnessAdapter`/`HarnessSession`.
- Import into an existing conversation, merge/conflict resolution, archive formats, encryption,
  signing, streaming/multipart upload, remote URLs, bulk import/export, or backup/restore
  guarantees.

## Assumptions

- The owner is the retention-policy principal; delegated/shared ownership and organization-wide
  policies are not part of the current user model.
- A 1–120 calendar-month range is sufficient for the first configurable policy. Infinite retention
  for one active conversation is expressed only by its exemption flag.
- The existing sanitized canonical text boundary is suitable for owner-visible snippets and
  transcript export. Phase 11 does not introduce a second secret detector.
- Clients that need highlighted UI can use `matched_terms`; server-rendered HTML and exact character
  offsets are unnecessary for the current Python/JSON API.
- Transcript portability means semantic canonical context, not lossless reproduction of provider
  history. Seeding may cause a provider-generated acknowledgement that is drained and discarded,
  exactly like existing switching/recovery handoff seeding.
- A crash after native candidate creation but before the import transaction may leave an abandoned
  provider-side session. The package closes owned local resources but makes no provider-deletion
  claim.

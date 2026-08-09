# Search, Retention, and Transcript Portability

Phase 11 product surface for ranked conversation search, owner-scoped retention,
and canonical transcript export/import. None of these features expose or delete
provider-native sessions or workspace files.

## Search

`GET /api/v1/conversations/search` and `TalkToHarnessesService.search_conversations`
accept `q`, `cursor`, and `limit` only. Successful results are
`Page[ConversationSearchHit]` (`conversation` shell plus optional plain-text
`snippet`).

### Query grammar

Supported:

- unquoted positive terms and double-quoted positive phrases;
- `-term` and `-"quoted phrase"` exclusions;
- `is:pinned`, `is:archived`, `has:interaction`;
- `harness:grok|cursor|codex|claude|opencode`;
- `before:YYYY-MM-DD` (exclusive 00:00 UTC) and `after:YYYY-MM-DD` (inclusive 00:00 UTC)
  applied to `updated_at`.

Terms are implicit AND. At least one positive term or phrase is required.
Unknown operators, unclosed quotes, empty phrases, invalid dates/kinds,
conflicting filters, more than eight text clauses, or a query longer than 512
Unicode code points raise `invalid_search_query` (HTTP 400).

Not supported: `OR`, parentheses, wildcards, field selectors, fuzzy matching,
stemming, autocomplete, saved searches, or raw SQL/FTS syntax.

### Ordering and snippets

Matches are ranked by a fixed integer formula over normalized title and body
fields (title clause/term weights, body phrase/term weights with caps). Order is
rank descending, then `updated_at` descending, then conversation UUID descending.
Opaque cursors carry those three values plus a query digest; reusing a cursor
with a different normalized query is rejected.

Snippets are at most 240 code points of sanitized plain text around the first
positive clause in the retained body. `matched_terms` lists distinct query terms
present in that snippet, in query order. Title-only matches return
`snippet=null`. The server never returns HTML.

PostgreSQL and SQLite implement the same grammar, match set, relevance formula,
tie-breaks, and cursor semantics. Backend FTS indexes are private derived indexes
over the sanitized search document.

## Retention

Retention remains externally scheduled through
`python manage.py talktoharnesses_cleanup`. There is no in-process scheduler and
no HTTP endpoint that executes cleanup.

### Owner policy

- `GET /api/v1/retention` / `get_retention_policy` — effective policy.
- `PUT /api/v1/retention` with `{"months": N}` / `replace_retention_policy` —
  upsert, `1 <= N <= 120`.
- Absent policy means six calendar months and `updated_at=null`.
  `PUT {"months": 6}` restores default behavior without a delete endpoint.

Cutoffs use calendar months in UTC (month-end and leap-year clamping), never a
fixed day count. Policy changes affect future cleanup passes only; they do not
rewrite timestamps or run cleanup inside the HTTP request.

### Exemptions and preview

- `PUT /api/v1/conversations/{id}/retention-exemption` with `{"exempt": true|false}`
  protects turn history for one live conversation. Soft-deleted conversations
  still purge after the owner's configured period. Approval audits keep their
  existing indefinite retention.
- `retention_exempt` appears on the full `Conversation` (detail/snapshot), not on
  `ConversationShell`.
- `GET /api/v1/retention/preview` reports eligible counts (`cutoff`, soft-deleted
  conversations, history conversations, terminal turns, waiting turns) using one
  captured database-consistent `now`, without mutation.
- `talktoharnesses_cleanup --dry-run` aggregates the same preview across owners
  and prints fixed-field counts without mutation.

Workspace files and provider-side sessions are never deleted by retention.

## Canonical transcripts

Provider-neutral document format:

- `format`: `talktoharnesses.canonical-transcript`
- `version`: `1`
- `title` plus ordered `turns` of user/assistant messages and canonical tools

Excluded from v1: conversation/turn/message/tool UUIDs, owner IDs, harness kind,
bindings, native session IDs, timestamps, interactions, plans, reasoning,
raw/native events, stderr, full tool output, usage/cost, and workspace files.

### Export

`GET /api/v1/conversations/{id}/transcript` /
`export_transcript(owner_id, conversation_id)` returns the deterministic JSON
document for retained history. Deleted content cannot be recovered from events
or native sessions after pruning. File form uses canonical JSON serialization
plus one trailing newline; HTTP remains JSON without content-disposition.

### Import

`POST /api/v1/conversations/import` /
`import_transcript(owner_id, harness_id, document)` validates and redacts the
document, converts it to the internal handoff representation, seeds a transient
candidate runtime, and only then commits a new conversation, binding, imported
rows, search document, and one `transcript_imported` event. Failure leaves no
durable conversation state. The response is `201` with a normal
`ConversationSnapshot`.

Imports execute the transcript as a handoff prompt in the selected local harness
under the authenticated user's normal workspace permissions. Import is not a
sandbox or a trusted backup restore. Import never attaches to an existing
conversation, reuses source IDs, or exposes native identifiers.

Limits include at most 5,000 entries and a 5 MiB canonical JSON representation.

# Phase 6 — Persistent Approvals and Complete Interactions

## Summary

- Merge the completed Phase 5 branch first, then implement Phase 6 as version
  `2026.8.0.dev6`.
- Complete the existing interaction lifecycle for multiple simultaneous approvals and structured
  questions. Requests, drafts, submitted answers, audits, commands, and canonical events remain
  durable and owner-scoped.
- Add one provider-neutral `InteractionBroker` in the application layer. Adapters retain only
  native request responders and translation; they do not query rules or decide policy.
- Add persistent allow/deny rules for exact command arguments, exact file operations, recursive
  directory operations, and blanket network access. All applicable denies override all allows.
- Preserve the Phase 4 adapter methods and the Phase 5 interaction routes. Extend the existing
  domain, persistence, facade, command-worker, and API paths instead of adding a second interaction
  system.
- Add no dependency, generic policy engine, role system, approval DSL, or provider-specific rule
  table.

## Phase Boundary and Preconditions

Phase 6 starts only after the Phase 5 gate passes. Before adding rules, reconcile the merged
implementation with these existing contracts:

- `InteractionRequestedPayload`, `PendingInteraction`, `InteractionAnswer`,
  `InteractionProjection`, and the interaction draft/resolution transitions remain the canonical
  interaction vocabulary. Phase 6 may extend them with provider-neutral fields, but must not create
  separate Grok approval models.
- The Phase 4 event pump must atomically persist an interaction request before an adapter can be
  answered. Phase 6 makes interaction requests force-flush boundaries rather than relying on the
  ordinary 50 ms delta window.
- The Phase 5 facade and HTTP API must already enforce owner scoping for conversation and nested
  interaction IDs. Rule and audit operations follow the same missing-or-cross-owner behavior.
- `answer_interaction` remains the only provider-delivery command. The command processor must
  settle that command after the adapter has flushed its native response; it must not wait for a
  turn-terminal event to settle an interaction answer.
- The committed-event broker remains a wakeup transport, not the event source. Phase 6 adds a
  narrow publication gate only for interaction resolution: a provider cannot receive an answer
  before the matching canonical resolution event has been committed and passed to the publisher.

The Phase 5 `resolve_interaction()` implementation currently commits the aggregate/events and then
writes `InteractionAnswerRecord` separately. Replace that split write with the Phase 6 atomic
resolution operation. Do not keep both paths.

The current conversation binding copies a harness configuration but does not retain the configured
`HarnessInstance` ID. Add `harness_instance_id` to `ConversationHarnessBinding` and populate it when
Phase 5 creates a conversation. Harness-instance rule scope reads this immutable source ID; it must
not infer identity by comparing mutable configuration JSON. For databases upgraded from Phase 5,
backfill only when one owned harness is an unambiguous exact match. A legacy binding without a
source ID simply cannot match harness-instance rules.

General process-crash recovery, including a native provider request that was waiting when its
process disappeared, remains Phase 9. Phase 6 guarantees durable local resolution and at-most-once
delivery; it does not claim that an extinct native responder can be reconstructed.

## Public and Internal Contracts

### Provider-neutral interaction broker

Add one `InteractionBroker` under `talktoharnesses.application`. The service constructs it from the
existing `Persistence`, `CommittedEventPublisher`, and UTC clock and passes the same instance to the
command processor. The broker owns these operations:

- accept and force-commit a normalized interaction request;
- publish the committed request event before evaluating automatic policy;
- evaluate persistent rules for approval interactions;
- update an editable draft through the existing transition;
- resolve an interaction manually or automatically with first-write-wins semantics;
- cancel open interactions during interrupt handling; and
- release a durable `answer_interaction` command only after the committed resolution event has
  been published.

`TalkToHarnessesService` delegates its interaction methods to this broker. The command processor
delegates normalized provider requests and interrupt-time cancellation to the same broker. No
adapter, route handler, or Django model reimplements resolution or rule selection.

Adapters continue to implement the exact Phase 1 `HarnessAdapter` methods, including
`answer_interaction(session, answer)`. Add one provider-neutral `HarnessInteractionRequest`
adapter-event envelope containing the canonical `InteractionRequestedPayload` and opaque provider
correlation identifiers. It becomes the sole noncanonical input accepted by the broker; the event
pump persists only its payload as a `ConversationEvent`. Correlation identifiers are stored in the
interaction/audit rows but excluded from SSE, snapshots, and public interaction projections.

### Domain models

Add only the wire-stable enums and frozen Pydantic models required by the milestone:

- `ApprovalRuleDecision`: `allow` or `deny`. Do not reuse `ApprovalDecision`, whose
  `allow_once`, `allow_session`, `deny`, and `cancel` values describe an immediate provider answer.
- `ApprovalRuleScopeKind`: `conversation`, `harness_instance`, `executable`, `user`, or
  `principal_global`.
- A discriminated `ApprovalRuleScope` union whose scope value is required for every variant except
  `principal_global`.
- A discriminated `ApprovalAction` union for normalized command, file, and network requests.
- A discriminated `ApprovalMatcher` union for exact argv, exact path operation, recursive directory
  operation, and blanket network access.
- `ApprovalRule`, `ApprovalRuleProjection`, and `InteractionAuditProjection`.
- A resolution result containing the winning `InteractionAnswer`, its durable command projection,
  and whether this caller performed the first write. Duplicate callers receive the original result
  without another event, audit, rule, or command.

Keep approval request presentation fields such as tool name and summary. Add normalized action
data and the provider-neutral available immediate decisions only where a provider fixture proves
them:

| Normalized request action | Required data | Matching rule |
| --- | --- | --- |
| command | non-empty ordered argument tuple | exact argv |
| file | normalized path and one `FileOperation` | exact path or recursive directory with the same operation |
| network | explicit network-access marker | blanket network |

An approval with only display text remains manually resolvable but cannot be automatically matched.
Do not parse summaries or shell strings to manufacture argv, paths, operations, hosts, or network
intent.

`InteractionAuditProjection` covers approvals and structured questions so every submitted outcome
has one immutable audit. It includes audit ID, owner/principal ID, interaction/conversation/turn
IDs, interaction kind, decision or structured answers, automatic/manual origin, timestamps,
provider request identifiers, deciding rule ID when present, and copied scope/matcher/decision
snapshots. Raw native frames and secrets are not exposed.

### Principal and scope semantics

Every rule belongs to the authenticated principal that created it. Phase 6 does not add delegation,
roles, administrators, or cross-user rule management.

- `conversation` applies to one owned conversation UUID.
- `harness_instance` applies to conversations created from one owned configured harness UUID.
- `executable` applies when the current immutable launch snapshot has the same resolved executable.
- `user` applies to conversations owned by the specified user. The authenticated API may specify
  only itself.
- `principal_global` has no scope value and applies to every conversation acted on by that
  principal.

In the Phase 5 authentication model, the Django user is both principal and resource owner, so
`user` and `principal_global` usually cover the same conversations. Preserve their distinct stored
meaning without adding a principal framework to make them artificially different.

Rule creation verifies conversation and harness-instance scope IDs through existing owner-scoped
lookups. It normalizes executable scope with the existing strict executable resolver; evaluation
compares that value with the immutable launch snapshot and never re-resolves a different configured
path.

### Immediate decision semantics

Validate answers by interaction kind:

- Approval interactions accept exactly one of `allow_once`, `allow_session`, `deny`, or `cancel`
  and no structured answers.
- Structured questions accept answers and no approval decision.
- `allow_session` is a manual provider-native decision. It does not create or mutate a package
  rule.
- An automatically matched allow is delivered to the provider as `allow_once`; the package rule,
  not provider session state, remains the durable source of future decisions.
- An automatically matched deny is delivered as `deny`.
- “Create rule and allow” accepts an allow rule that matches the current normalized request and
  submits `allow_once` for the current interaction.

Reject invalid combinations before any persistence mutation. `cancel` is a submitted outcome, not
deletion of the interaction. A manual decision must be present in the request's normalized
available-decision set. The pinned adapter contract must always provide mappings for automatic
`allow_once` and `deny`; an interaction shape that cannot represent those outcomes is not eligible
for package rule automation.

## Matching Rules

Put normalization and selection in one pure domain module and use it from broker, persistence
transactions, and tests. Rule use is read-only: evaluation never changes a rule, increments a use
counter, widens a matcher, or updates a last-used timestamp.

### Matcher normalization

- Exact argv compares the complete tuple element-for-element. Argument order, element boundaries,
  empty-string arguments, case, quoting characters, and executable spelling remain significant.
  Never join argv into a shell string.
- Resolve provider-relative file paths against the immutable launch working directory. Resolve
  symlinks in existing path components and normalize platform case rules. A create target may have
  a nonexistent final component.
- Standalone exact-path rule creation requires an absolute path. “Create rule and allow” may copy
  the already normalized path from the current request.
- A recursive-directory rule stores an existing resolved directory and one file operation. Match
  containment by path components, not string prefix, and require the operation to be identical.
- An exact-path rule requires both the resolved path and operation to be identical.
- Blanket network rules match only an explicit normalized network action. They do not match command
  requests merely because an argv appears to invoke a network tool.

Reuse the existing runtime executable/directory resolution where its semantics match. Add one
approval-path helper for the create-target case that `resolve_directory()` intentionally rejects;
both request and rule normalization call that helper.

### Scope and decision selection

Load every rule owned by the principal whose scope applies to the current interaction context, then
apply its matcher. Do not stop at the first row returned by the database.

Use this specificity order only to select the deciding rule for a stable audit snapshot:

1. conversation;
2. harness instance;
3. executable;
4. user;
5. principal-global.

For file matchers, exact path is more specific than recursive directory. Exact argv and blanket
network each have one specificity level. UUID is the final deterministic tie-breaker.

Decision precedence is independent of specificity:

1. If any applicable matching deny exists, deny. Record the most specific matching deny.
2. Otherwise, if any applicable matching allow exists, allow. Record the most specific matching
   allow.
3. Otherwise leave the interaction pending for a manual answer.

A conversation-specific allow therefore cannot override a principal-global deny. This is the one
deny-wins rule used by automatic evaluation and by previews/tests; no API or adapter may implement
a different precedence.

Rule create and replacement validate scope ownership and normalize matcher values before writing.
Conflicting rules are allowed because deny-wins is meaningful only when more than one rule can
match. Do not add a uniqueness constraint that silently removes those conflicts.

## Work Package 1 — Complete Interaction State Transitions

Extend the existing pure transitions instead of replacing them:

- `request_interaction` accepts more than one distinct pending interaction for the active turn.
  A duplicate canonical interaction ID with byte-identical request data is idempotent; conflicting
  reuse is `invalid_state`.
- A turn enters `waiting` when its first open interaction is requested and remains waiting while
  any interaction is pending or in draft.
- Draft updates are allowed only for pending/draft interactions. A submitted, resolved, or
  cancelled interaction is immutable.
- The first submitted answer wins. It records the immutable answer and audit intent. Later manual,
  automatic, or cross-worker submissions return the winning result without mutation.
- Resolving one of several open interactions does not resume the turn. Resolving/cancelling the
  final open interaction returns the active turn and conversation to `running`.
- Explicit turn interruption cancels every still-open interaction. The cancellation events are
  committed and published before the adapter's interrupt method resolves native waiters.
- A turn-terminal transition must not leave pending/draft interactions behind. Use the same
  cancellation transition rather than directly rewriting statuses.

Emit one `interaction_requested` event per accepted request, one draft event per committed edit,
and one `interaction_resolved` event per winning submission. Keep the current `automatic` flag and
event shape for manual and rule-driven outcomes. Deciding rule snapshots and provider request IDs
remain private to the audit.

Do not add interaction deadlines, timeout decisions, default allows/denies, or single-interaction
serialization. Waiting is indefinite until a submitted outcome, interrupt, provider termination,
or an existing lifecycle failure closes the turn.

## Work Package 2 — Broker Commit and Delivery Ordering

### Request acceptance

When an adapter emits an interaction request, the command processor passes it to the broker under
the existing per-conversation lock:

1. Load the latest worker snapshot and apply `request_interaction`.
2. Atomically persist the aggregate, relational interaction projection, private provider
   identifiers, and canonical request/waiting events.
3. Force the batch boundary and pass the returned committed events to the publisher.
4. Only after successful publication, evaluate rules in a database transaction against the
   committed interaction and current rule rows.
5. If no rule matches, return and leave the provider responder pending indefinitely.
6. If a rule matches, execute the same resolution path used by a manual answer.

An interaction request is never auto-resolved from uncommitted state. Other streaming deltas may
still use the Phase 2 50 ms accumulator.

Persist the request-event sequence and a policy-evaluated timestamp on the private interaction row.
If publishing fails, leave the timestamp empty. At service startup and after publisher recovery,
the broker republishes the committed request event and evaluates each still-open, unevaluated
interaction once. A completed no-match evaluation sets the timestamp transactionally, so creating
a standalone rule later does not retroactively answer an already pending request. The user may
still resolve that request manually.

### First-write-wins resolution

Replace the old narrow `resolve_interaction()` protocol method with
`commit_interaction_resolution()`. In one database transaction it must:

1. Owner-scope and lock the conversation and interaction rows.
2. Return the existing winner if an immutable answer already exists.
3. For automatic resolution, lock applicable live rules, run the one pure selector, and copy the
   deciding rule into the audit.
4. For “create rule and allow,” validate the proposed rule against the locked interaction context
   and insert the live allow rule.
5. Apply the interaction transition and optimistic conversation version update.
6. Insert the unique immutable answer and one immutable audit row.
7. Materialize interaction/turn/conversation state and append canonical resolution events.
8. Record the resolution event sequence as awaiting publication.

The unique interaction answer is the database first-write-wins authority. An optimistic aggregate
conflict must not turn a duplicate submission into a second winner.

The rule, copied audit snapshot, answer, aggregate state, and canonical resolution events commit or
roll back together. A concurrent losing “create rule and allow” call must not leave an unused rule
behind.

### Publication-gated command release

Do not make an `answer_interaction` command claimable in the resolution transaction. The broker:

1. publishes the exact committed resolution event;
2. in a second atomic operation, creates or returns the one answer command for that interaction,
   attaches it to the aggregate, and marks the resolution released; and
3. returns that durable command projection to the facade or leaves it for the worker after an
   automatic decision.

Persist the resolution event sequence and release state on the private answer row. On service
startup, and after a publisher error, reconcile unreleased resolutions by loading the committed
event, publishing it again, and idempotently releasing the command. Repeated publication is safe
because live consumers deduplicate by conversation sequence and replay remains authoritative.

This is a focused interaction-resolution outbox, not a generic message bus or replacement for the
Phase 5 event broker.

The other new persistence operations stay equally coarse:

- `commit_interaction_request()` persists the transition plus private correlation/publication
  state;
- automatic mode on `commit_interaction_resolution()` locks candidate rules and either commits the
  winner or marks the completed no-match evaluation in that same transaction;
- `release_interaction_answer()` creates/returns the unique command after publication;
- reconciliation reads return only unevaluated open requests and unreleased submitted answers; and
- owner-scoped rule/audit create, replace, delete, get, and page operations return domain
  projections, never querysets.

The command processor marks delivery started before calling the adapter. After
`answer_interaction()` returns—meaning the native response frame/callback was flushed—it marks the
command delivered and settled in one update. If the process or worker fails after delivery starts,
the command becomes `outcome_unknown` and is never automatically retried. This preserves at-most-
once provider delivery even though local resolution remains durable.

## Work Package 3 — Django Rule and Audit Persistence

Add one Phase 6 migration with private relational models; ORM models remain outside public Python
API.

### `ApprovalRuleRecord`

Store:

- UUID primary key;
- principal/owner ID;
- rule decision;
- normalized scope kind and scope data;
- normalized matcher kind and matcher data;
- created and updated timestamps.

Index `(principal_id, scope_kind)` for candidate loading and `(principal_id, -created_at, -rule_id)`
for API pagination. Do not add use counters or require rule uniqueness.

Explicit replacement updates only the live rule after ownership validation. Evaluation locks and
copies the row it used, so a concurrent replacement affects either the current interaction or the
next one as a whole, never half of an audit. Hard deletion removes the live row. It does not delete
or rewrite prior audits.

### `InteractionAuditRecord`

Store immutable copied data rather than depending on live foreign-key joins:

- audit, owner/principal, interaction, conversation, and turn IDs;
- interaction kind, winning decision/answers, automatic flag, and timestamps;
- provider kind plus redacted provider request identifiers;
- deciding live rule ID when one existed;
- copied rule decision, scope, and matcher; and
- the normalized request action used for the decision.

The optional live-rule relation uses `SET_NULL`, while the copied UUID and JSON snapshots remain.
Conversation soft deletion and rule hard deletion do not erase audits. Phase 8 retention policy may
later define when audit rows themselves expire; Phase 6 does not add cleanup.

### Existing interaction rows and constraints

Extend `InteractionRecord` with private provider-correlation data, resolution metadata, and the
request event sequence/policy-evaluated timestamp needed for broker reconciliation. Extend
`InteractionAnswerRecord` with the conversation/interaction relationship, unique command
relationship, resolution event sequence, and release timestamp. Preserve the unique interaction ID
as the first-write-wins constraint.

Add a database uniqueness constraint proving at most one `answer_interaction` command belongs to
an interaction. Do not rely only on the derived idempotency-key string.

Run the same rule, audit, and interaction-resolution persistence contract against SQLite and
PostgreSQL. Django transaction bodies remain synchronous and cross the existing thread-sensitive
async boundary.

## Work Package 4 — Rule CRUD and Audit Facade/API

Extend `TalkToHarnessesService` with owner/principal-scoped methods:

- create, list, get, replace, and hard-delete an approval rule;
- list and get immutable interaction audits; and
- resolve an interaction with an optional “create allow rule” specification.

Reuse Phase 5's keyset cursor codec. Rules and audits order by `(created_at DESC, id DESC)`, default
to 50 items, and cap at 200. Return shared Pydantic projections from both Python and HTTP. Do not
expose ORM models, raw provider payloads, or a rule-evaluation endpoint.

Add these authenticated routes under `/api/v1`:

| Method and path | Behavior |
| --- | --- |
| `GET /approval-rules` | Keyset-page the caller's live rules. |
| `POST /approval-rules` | Create a normalized owner-scoped rule and return 201. |
| `GET /approval-rules/{id}` | Return one owned live rule. |
| `PUT /approval-rules/{id}` | Replace its decision, scope, and matcher as one validated value. |
| `DELETE /approval-rules/{id}` | Hard-delete only the live rule and return 204. |
| `GET /interaction-audits` | Keyset-page immutable outcomes for the caller. |
| `GET /interaction-audits/{id}` | Return one owned audit snapshot. |

Keep the existing conversation interaction routes. Extend the resolve body with an optional full
allow-rule specification. It is valid only with an approval decision of `allow_once`, only when its
matcher matches the current normalized request, and only when its scope contains the current
interaction context. The route still returns the durable command projection with 202 after the
resolution event has been published and the command released.

Rule create/replace validation failures return 422. Missing, cross-owner, or deleted rule/audit IDs
return the same generic 404. A losing first-write-wins submission returns the original command; it
does not return a new conflict merely because another worker won. A proposed create-and-allow rule
that loses the race is not created.

The Python facade continues to accept explicit owner/principal IDs. The Django routes derive them
only from `request.auth`; request bodies never choose another principal.

## Work Package 5 — Grok Interaction Completion

Keep Grok-specific work limited to strict decoding, normalized request extraction, private native
correlation, and answer mapping:

- Expand the allowlisted `session/request_permission` schema only for fields present in pinned Grok
  fixtures. Do not leave permission `params` as an arbitrary dictionary after Phase 6.
- Capture the JSON-RPC request ID and tested tool-call/provider IDs as private correlation data.
- Normalize exact argv, file path/operation, or explicit network intent only from typed fixture
  fields. A permission shape without such data remains manual-only.
- Normalize the request's advertised native options to the canonical available-decision set so the
  broker rejects an unavailable manual decision before committing it.
- Retain one pending native responder per canonical interaction ID. Multiple reverse requests may
  be pending at once and may be answered in any order.
- Map the four immediate canonical decisions only to options actually advertised on that request.
  If a requested decision has no equivalent native option, reject it before popping or responding
  to the native waiter.
- Pop a native responder exactly once after its response is flushed. A second delivery is an
  interaction-resolution error, never another JSON-RPC response.
- On interrupt, use the broker to durably cancel and publish all open interactions before Grok sends
  cancelled outcomes and `session/cancel`.

Run the existing Grok permission fixtures through the broker rather than testing adapter mapping in
isolation only. Phase 7 adapters must use this same broker contract; Phase 6 does not implement
those adapters early.

## Test Plan

### Pure domain and matcher tests

- Table-test legal answer shapes for approvals/questions and reject mixed, empty, or wrong-kind
  submissions.
- Request two or more interactions for one turn, draft/resolve them in different orders, and prove
  the turn remains waiting until the final open interaction is resolved.
- Race manual/manual, manual/automatic, and automatic/automatic answers and assert one immutable
  winner, event, audit, rule side effect, and command.
- Distinguish `("tool", "a b")` from `("tool", "a", "b")`, argument order, case, and empty
  arguments.
- Cover relative and absolute path normalization, existing symlinks, nonexistent create leaves,
  path-component containment, sibling-prefix directories, platform case behavior, and each
  `FileOperation`.
- Prove blanket network rules match explicit network actions only.
- Cover every scope boundary and the deterministic specificity order. Include a less-specific deny
  against a more-specific allow and prove deny wins.
- Verify matcher and scope unions reject unknown variants/fields and invalid value combinations.

### Broker and persistence tests

- Prove request rows/events commit before rule lookup and that publisher failure leaves the native
  responder pending.
- Prove resolution, optional allow-rule creation, audit snapshot, aggregate/event update, and answer
  row commit or roll back together on SQLite and PostgreSQL.
- Race duplicate answers and create-and-allow calls from separate persistence instances/workers.
- Delete or replace a rule concurrently with evaluation and assert the audit contains one coherent
  before-or-after snapshot.
- Delete a live rule and soft-delete its conversation; assert copied audit data remains queryable by
  its owner.
- Fail after resolution commit, after event publication, after command release, before native
  write, and after native flush. Reconciliation may republish/release, but provider delivery occurs
  at most once.
- Verify an automatic request and resolution are committed and published in sequence before
  `answer_interaction()` is called.
- Reconstruct persistence and the service with unresolved, resolved-but-unreleased, and released
  interactions. Assert an unevaluated request is published/evaluated once and only the
  resolved-but-unreleased case creates its one missing command.

### Facade and HTTP tests

- Reuse identical expected Pydantic objects for facade and HTTP rule/audit serialization.
- Cover rule CRUD, audit list/get, cursor ordering, page limits, create-and-allow, all four immediate
  decisions, structured answers, drafts, and duplicate resolution responses.
- Run every rule, audit, nested interaction, and create-and-allow endpoint with two users. Cross-user
  identifiers and scopes must be indistinguishable from missing resources.
- Assert create-and-allow returns only after publication and returns the same durable command on a
  duplicate call.
- Verify OpenAPI authentication, strict request unions, status codes, and absence of private
  provider identifiers from interactions, events, snapshots, and error bodies.

### Grok and end-to-end gate

- Add strict fixtures for every supported permission option, concurrent permission requests, typed
  argv/path/network requests present in the pinned protocol, missing manual-only matcher data,
  answer ordering, interrupt cancellation, and unknown fields.
- Exercise each applicable fixture manually and through allow and deny rules. Assert identical
  canonical request/resolution event shapes apart from `automatic` and deciding rule ID.
- Start the packaged ASGI surface with the Grok fixture peer, create a rule through HTTP, submit a
  turn, observe request and automatic resolution over SSE, and prove the peer receives one native
  response only after both events are durable/published.
- Repeat the transactional race and publication-order contract in the PostgreSQL CI job.
- Gate with Ruff, format check, strict Pyright, full tests, lockfile check, migration drift check,
  wheel/sdist builds, and isolated core imports without Django.
- Phase gate: every approval and structured-question outcome is durable, owner-scoped, auditable,
  and delivered to the active provider responder at most once; persistent allow/deny decisions are
  deterministic and deny-wins.

## Implementation Order

1. Reconcile the Phase 5 interaction preconditions and add the harness-instance source ID.
2. Define normalized rule/scope/audit models and pure matcher/transition tests.
3. Add the Django migration and common SQLite/PostgreSQL first-write-wins persistence contracts.
4. Implement the broker request, resolution, publication-release, and reconciliation paths.
5. Route existing facade/command-processor interaction behavior through the broker and settle
   answer commands after native flush.
6. Add rule/audit facade methods and thin authenticated API routes.
7. Complete strict Grok permission normalization and run manual/rule-driven fixture and end-to-end
   gates before changing the package version to `2026.8.0.dev6`.

## Explicitly Out of Scope

- Cursor, Codex, Claude Code, and OpenCode adapters; Phase 7 plugs them into the completed broker.
- Harness switching, transcript handoff, PostgreSQL/FTS5 search, enriched projections, title
  recomputation, and retention cleanup from Phase 8.
- General crash takeover, native responder reconstruction, ambiguous prompt recovery, executable-
  change fallback, OpenTelemetry, and fault injection from Phase 9.
- Rule import/export, groups, teams, delegation, administrator overrides, role-based policy, policy
  expressions, regex/glob argv, shell parsing, environment-variable rules, per-host/port network
  rules, time-based rules, counters, expiry, priorities, prompts generated from rules, or automatic
  rule learning.
- Provider-managed persistent approval configuration. Package rules answer each matched provider
  request explicitly and remain the only package-owned policy source.

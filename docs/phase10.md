# Phase 10 — Release Readiness

## Summary

- Start Phase 10 only after the complete Phase 9 gate passes on SQLite, PostgreSQL, Linux, macOS,
  and Windows. Keep the package at `2026.8.0.dev9` while release work is in progress; there is no
  `2026.8.0.dev10` milestone.
- Stabilize the existing five-adapter product. Phase 10 may fix a release-blocking defect in an
  already documented contract, but it does not add product behavior, provider capabilities,
  persistence concepts, or migrations.
- Publish compatibility claims only for exact create and resume combinations exercised by the
  opt-in live suites. A known release is not a support claim.
- Make the generated support document, package metadata, public Python exports, deployment and
  upgrade documentation, and built distributions agree with the same release state.
- Split CI by responsibility: static checks, coverage, provider contracts, database behavior,
  operating-system runtime behavior, performance, and distribution verification. Do not run the
  entire suite redundantly in every job.
- Build the wheel and sdist once from a locked checkout, verify those exact artifacts, and land the
  tag/publish workflow that will publish those same bytes through `uv publish` and PyPI trusted
  publishing. The actual live-evidence matrix population, 91% coverage closeout, `2026.8.0`
  metadata cut, tag, and PyPI publication are owned by [Phase 12](phase12.md).
- Keep the package and adapter compatibility versions at `2026.8.0.dev9` for the Phase 10 merge.
  Do not invent matrix rows or cut the stable version without live evidence.
- Do not add a package CLI, automatic migrations, provider installers, compatibility ranges,
  deployment templates, a telemetry SDK/exporter, or Phase 11/12 product-or-publication work beyond
  the machinery above.

## Current Baseline and Entry Conditions

Phase 10 begins from the merged Phase 9 result, not from a partially passing release branch. Before
release work starts, verify all Phase 9 recovery, fencing, secret-sink, readiness, and shutdown
tests are present and green. A Phase 9 failure is fixed in its owning implementation; it is not
reclassified as a release exception.

The current checkout has these Phase 10 gaps:

- `pyproject.toml` and all five compatibility documents use `2026.8.0.dev9`. The README still
  describes an early pre-adapter scaffold and does not describe the shipped facade, API, adapters,
  operational profiles, or recovery behavior.
- Grok, Cursor, Codex, Claude Code, and OpenCode each have one known release record, but every
  `create_matrix` and `resume_matrix` is empty. The current string entries can identify a release
  but cannot identify the operating system on which create or resume passed.
- Matrix-membership helpers exist in several provider modules, but the rule is repeated, Claude
  has no equivalent helper, and adapter start/resume paths do not consistently enforce the
  published operation-specific matrix.
- Opt-in live tests exist for Cursor, Codex, Claude Code, and OpenCode, but they exercise create
  only despite their module descriptions. There is no live Grok test. They do not yet prove a
  fresh adapter/runtime can resume a retained native session and complete another turn.
- `SUPPORTED_HARNESSES.md` is generated and a unit test detects drift, but CI has no named direct
  regeneration check and the generated matrix sections cannot show exact platform rows.
- Wheel/sdist smoke tests cover core and Django wheel installs. They do not yet install the `all`
  extra, install the sdist, or assert that compatibility JSON, Django migrations, `py.typed`, and
  all required optional-dependency metadata are in the artifacts.
- The existing OS matrix repeats lint, format, Pyright, coverage, and build work three times. It
  has no enforced coverage percentage, dedicated performance job, release workflow, or tag/version
  consistency check.
- Public `__all__` declarations exist, but provider packages expose compatibility implementation
  models and helpers alongside adapters, and the internal ACP package presents an export list even
  though it is not a public generic ACP client.
- The roadmap summary still names an `otel` extra, while the later Phase 9 dependency contract
  deliberately makes `opentelemetry-api` a core dependency and leaves the SDK/exporters to the
  host. The detailed Phase 9 contract is authoritative: correct the summary and documentation;
  do not add an empty or SDK-owning `otel` extra.

Do not populate a matrix from fixture coverage, inferred version similarity, an adapter probe, or
a developer's recollection. If an exact release cannot pass the required public-contract live
suite—currently a known risk for Codex approval handling—the stable release remains blocked. Do not
use private SDK attributes, auto-approval, or a reduced capability claim to force the milestone.

## Release Invariants

These rules are the source of truth for Phase 10 implementation and release automation:

1. A compatibility release row is a strict implementation candidate. A create or resume support
   claim exists only as a matrix entry containing that release ID and one exact platform.
2. Each stable adapter has at least one published create entry and one published resume entry. A
   resume entry references a release whose capability record advertises resume.
3. Every matrix entry references exactly one release, uses a platform listed by that release, and
   corresponds to a successful live workflow run for the same package revision and native version
   tuple. Duplicate entries are invalid.
4. Adapter create and resume enforce their separate published matrices. Create support never
   implies resume support, and support on one operating system never implies another.
5. Unknown native versions, unlisted matrix combinations, missing optional dependencies, and
   missing executables continue to fail closed with existing public error codes. Phase 10 adds no
   compatibility fallback or unsafe override.
6. `pyproject.toml` remains the distribution-version source. Installed `__version__` comes from
   package metadata. Compatibility adapter versions and generated documentation must equal it at
   the stable gate; tests and fixtures must read installed metadata instead of hard-coding it.
7. `SUPPORTED_HARNESSES.md` is generated only from the strict packaged JSON models. No provider
   table, note, version, capability, or matrix entry is maintained by hand in Markdown.
8. The public Python surface is the explicitly approved set of names in package `__all__`
   declarations. Removing implementation-only pre-release exports is allowed in Phase 10; adding
   convenience aliases or a second public surface is not.
9. A release artifact is the exact wheel or sdist that passed distribution tests. The publish job
   downloads the build artifact and never rebuilds it.
10. The tag, project metadata, wheel metadata, sdist filename, installed `__version__`, all five
    adapter versions, and generated support document use the same stable version. A tag containing
    a development or local version is rejected.
11. Live-harness credentials, executable paths, prompts containing secrets, tokens, and provider
    output are never committed, uploaded as workflow artifacts, or printed by release checks.
12. Phase 10 introduces no ORM schema change. `makemigrations --check --dry-run` must remain clean,
    and upgrade documentation uses the existing forward migrations through Phase 9.

## Public Release Contracts

### Compatibility data and generated document

Keep one packaged JSON document per harness. Add one shared strict matrix-entry model with only:

```python
class CompatibilityMatrixEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    release_id: str
    platform: Literal["linux", "darwin", "win32"]
```

Use `list[CompatibilityMatrixEntry]` for both create and resume matrices in all five provider
documents. The provider-specific release record remains the one source for its exact CLI build,
SDK/runtime pair, protocol version, and capabilities; do not repeat those fields in each matrix
entry.

Add shared validation and membership functions in `providers.compatibility` and delete the five
copies of the same rule. Validation rejects unknown release IDs, duplicate `(release_id, platform)`
pairs, platforms absent from the release record, and resume rows for a release without resume
capability. Provider-specific version parsing remains provider-specific.

The renderer prints create and resume tables with release ID and platform. Exact provider version
details remain in the release table, so the generated document does not copy version tuples into a
second hand-maintained structure. Sort by fixed `HarnessKind` order, then release ID and platform,
and retain deterministic final-newline behavior.

### Python exports

Treat these package groups as the stable Python surface:

- `talktoharnesses`: installed `__version__` only.
- `talktoharnesses.domain`: canonical enums, models, projections, events, errors, fixture format,
  and pure transitions intentionally consumed by callers.
- `talktoharnesses.application`: persistence/publication protocols, redaction boundary, and the
  asynchronous `TalkToHarnessesService` facade.
- `talktoharnesses.providers`: adapter/session request protocols, fixed registry, and default
  registry construction.
- `talktoharnesses.runtime`: process/runtime lifecycle types already documented as public.
- `talktoharnesses.django`: `DjangoPersistence`; documented Django auth, ASGI, URL, and management
  command entry points remain importable from their defining modules.
- Provider packages: the concrete adapter class. Compatibility parsing, release-record models,
  render helpers, normalizers, wire schemas, and probes are implementation modules, not convenience
  exports.

Audit each current `__all__` against this table. Remove implementation-only names from provider
package exports and stop presenting ACP framing/JSON-RPC types as a supported generic client. Do
not rename internal modules merely to make them inaccessible, and do not create deprecated aliases
for pre-release exports.

Add a public-surface contract test that imports every approved name, verifies each `__all__` name
resolves, and verifies implementation-only names are absent from the relevant `__all__`. Keep this
test as the reviewed public-contract list; do not add runtime export discovery or API generation.

### Distribution extras

Document and test the extras already required by the product:

- `django` for Django, Django-Ninja, PyJWT, and the host-owned ASGI server dependency.
- `postgres` alongside `django` for Psycopg 3; SQLite needs no database extra but must provide FTS5.
- `grok` and `cursor` as intentionally Python-dependency-free markers for externally installed,
  explicitly configured executables.
- `codex` and `claude` for their exact SDK dependencies.
- `opencode` for the HTTP client used to supervise an external OpenCode executable.
- `all` for Django, PostgreSQL, and all five adapter dependency sets.

OpenTelemetry's API remains a core dependency and a no-op without host configuration. The host
installs and configures its chosen SDK and exporter packages separately. Do not add a package-owned
`otel` extra, SDK, exporter, collector, or settings surface.

## Work Package 1 — Exact Compatibility Evidence

### Shared schema and enforcement

1. Add the shared matrix-entry type and shared validation/membership functions.
2. Convert all five JSON documents and provider compatibility models from string lists to exact
   `{release_id, platform}` entries. Empty matrices remain valid only while the distribution
   version is a development version.
3. Make each adapter's existing `probe()` retain the matched release as it does now. Immediately
   before `start()` or `resume()` performs native work, validate that release plus `sys.platform`
   against the appropriate published matrix.
4. Preserve the existing development fixture seam explicitly: fake/unit adapters may bypass
   published-matrix enforcement through their existing test composition, but production default
   registry adapters may not. Do not make an empty stable matrix mean “allow all.”
5. Test malformed JSON, extra fields, duplicate entries, unknown releases, unsupported platforms,
   create-only releases used for resume, and operation/platform mismatches across all providers.
6. Update the renderer once for the shared entry type and retain the existing provider-specific
   release tables.

### Live create/resume gates

Complete one opt-in live module per provider, including Grok. When a live flag is enabled, missing
credentials, SDKs, executables, exact versions, or capabilities are failures rather than skips.
Each provider gate must:

1. Probe and assert the exact release identity represented by the proposed matrix row.
2. Start a fresh session through public adapter/runtime contracts.
3. Submit a unique deterministic prompt, consume normalized events through the authoritative
   terminal event, and close the first local adapter/runtime cleanly.
4. Construct a new adapter and, for process-bound providers, a new supervised process. Resume the
   retained native session ID rather than reusing an in-memory object.
5. Import the persisted native dedupe identity used by ordinary runtime resume, submit a second
   unique prompt, observe its authoritative terminal event, and assert the first turn is not
   replayed as a duplicate canonical event.
6. Exercise the provider's broker-compatible approval/question path required by its advertised
   capabilities. A provider that silently auto-approves or cannot defer an answer does not pass.
7. Interrupt or close any remaining activity and assert no owned task, client, responder, process,
   or descendant remains.

Use package metadata for `LaunchSnapshot.adapter_version`; remove hard-coded development versions
from live and common contract tests. Live tests may print fixed release IDs and pass/fail state, but
must not print prompts, credentials, environment values, native payloads, or executable paths.

After a live gate passes on a platform, add only that exact create/resume row to its JSON document.
Do not list all platforms merely because the release record recognizes them. If only Linux is
configured for the first stable release, only Linux is published.

### Stable compatibility gate

Add a release-only validation mode that fails unless:

- all five documents load strictly;
- all five adapter versions equal the installed stable package version;
- every adapter has at least one create and one resume row;
- every row passes shared referential/capability/platform validation;
- every published release remains pinned by exact native identity; and
- regenerating `SUPPORTED_HARNESSES.md` produces no diff.

Normal development CI runs structural validation and permits empty matrices while the package is a
`.devN` version. The tag/publish workflow always invokes the stable validation mode.

## Work Package 2 — Installation, Deployment, and Upgrade Documentation

### README and installation guide

Replace the pre-adapter README status with a concise stable overview and link to detailed operator
documents. Keep the quick start executable and cover:

- Python 3.11+, core-only installation, Django/SQLite, Django/PostgreSQL, individual provider
  extras, and `all`;
- the fact that Grok, Cursor, and OpenCode executables are external and are never discovered,
  installed, upgraded, or given arbitrary package-owned flags;
- provider SDK/executable versions being accepted only when listed in the generated compatibility
  matrix for the current operation and platform;
- host Django `INSTALLED_APPS`, URL inclusion, ASGI lifespan wrapper, migrations, and Uvicorn
  invocation on `127.0.0.1`;
- required JWT signing-key rules and trusted in-process `issue_token(user)` issuance; and
- the security boundary: authenticated submissions execute local harnesses with the Django OS
  user's workspace access and are not a sandbox.

Do not duplicate the complete support tables or operational guide in README. Link to
`SUPPORTED_HARNESSES.md` and the detailed documents.

### Deployment and operations guide

Add `docs/deployment.md` as the single operational guide. It covers:

- host settings, swappable user model, database configuration, migrations, URL/ASGI wiring,
  lifespan startup failure, `/health`, generic `/ready`, and graceful termination;
- one service/worker composition per ASGI process, PostgreSQL multi-worker fencing/failover, and
  SQLite's strict single-live-supervisor deployment profile;
- FTS5 as a SQLite prerequisite and PostgreSQL plus Psycopg as the multi-worker/recommended
  production profile, without claiming one database is universally required;
- creation of owner-scoped harness configurations and derivation of `owner_id` only from the
  authenticated Django user. Globally unique IDs never bypass owner filtering;
- JWT key separation, rotation/revocation behavior, one active token per user, and safe token
  handling. Do not add login, OAuth, or credential-storage flows;
- explicit executable paths, working-directory/additional-root ownership checks, provider
  authentication inherited from the service OS environment, and the fixed 20-runtime capacity;
- externally scheduling `python manage.py talktoharnesses_cleanup`, its fixed six-calendar-month
  database retention behavior, and its guarantee that workspace files are never deleted;
- optional host OpenTelemetry SDK/exporter composition and the fixed secret-safe, low-cardinality
  instrumentation boundary; and
- backup, logs/metrics, readiness, process termination, and database connectivity checks needed by
  an operator, without adding package-owned deployment automation.

Document recovery limits plainly: ambiguous delivery becomes `outcome_unknown` and is never
retried, a failed worker's live process/stdio is not adopted, uncommitted provider bytes may be
lost, native resume may fall back to a canonical retained handoff, SQLite has no multi-worker
takeover, and abandoned provider-native sessions are not deleted remotely.

### Forward upgrade guide

Add `docs/upgrading.md` with one conservative forward procedure:

1. Read the target compatibility matrix and release notes; verify provider versions and platform
   rows before changing the package.
2. Back up the relational database and retain the currently installed artifact/configuration.
3. Stop all old service processes cleanly and verify they are no longer ready. Mixed-version rolling
   upgrades are not supported for this first stable release.
4. Install the exact new wheel with the same required extras from a lock/constraints-controlled host
   environment.
5. Run `python manage.py migrate` once before starting new workers. The package never runs
   migrations automatically.
6. Start the lifespan-wrapped ASGI service, wait for `/ready`, probe configured harnesses, and run a
   harmless owner-scoped smoke conversation before restoring traffic.
7. Run the externally scheduled retention command only after the upgraded service is healthy.

Backward migration compatibility is not promised. After a forward migration, rollback means stop
the new processes and restore the pre-upgrade database backup together with the old artifact; do
not instruct operators to reverse migrations against production data.

### Live-test and release operator guide

Add `docs/live-testing.md` listing each opt-in flag, required executable/SDK/authentication setup,
the exact pytest selector, and how to compare the detected release to the proposed matrix row.
State that configured live tests fail on missing prerequisites, use disposable workspaces, may
incur provider cost, and must never run against an untrusted repository change with production
credentials.

Add `docs/releasing.md` as the ordered release checklist. It references successful live workflow
runs for all published rows, stable compatibility validation, performance/coverage results,
artifact verification, version bump, generated-document check, tag creation, protected-environment
approval, and post-publish installation smoke tests. It contains no credentials or mutable list of
supported versions.

## Work Package 3 — Public API and Distribution Audit

### Export audit

- Review `domain.__all__` name by name against the canonical Phase 1/5/6/8/9 public models and pure
  operations. Remove private recovery/fencing/observability helpers if any leaked during Phase 9.
- Keep `TalkToHarnessesService` lazy-exported so core application imports remain Django-free and
  cycle-free.
- Keep provider-neutral adapter protocols and registry types exported from `providers`; concrete
  adapters remain exported from their provider packages.
- Remove compatibility document classes, release record classes, matching/render functions, ACP
  transport types, and other implementation helpers from convenience `__all__` declarations.
- Verify removing an export does not lead to a duplicate public wrapper. Callers needing supported
  versions consume packaged JSON/`SUPPORTED_HARNESSES.md`, not compatibility implementation types.

Run public import tests in fresh interpreters with core-only, each provider extra, Django-only, and
`all`. Core/domain/application/providers/runtime imports must not load Django. Provider modules must
fail with their documented missing-dependency error only when the adapter operation needs the
dependency, not at module import.

### Artifact contents and install matrix

Extend distribution tests to inspect and install the actual release artifacts:

- The wheel contains all five compatibility JSON files, every Django migration through Phase 9,
  `py.typed`, package modules, and correct metadata/extras. It contains no tests, fixture secrets,
  coverage data, local paths, or development-only OpenTelemetry SDK.
- The sdist contains the source needed to reproduce the wheel plus `pyproject.toml`, README,
  license, typed marker, compatibility data, and migrations.
- A clean core wheel install imports core/domain/application/providers/runtime with no Django,
  PyJWT, Django-Ninja, Psycopg, provider SDK, or provider HTTP dependency pulled in accidentally.
- A clean Django-only wheel install initializes the app and passes Django system checks on SQLite
  without Psycopg or provider SDKs.
- A clean `all` wheel install initializes Django, imports every provider adapter, loads all five
  compatibility documents, and confirms PostgreSQL's driver is available.
- A clean core sdist install builds and imports successfully, proving the sdist is not missing build
  or package data.

Run isolated-install commands outside the repository import path so a source checkout cannot mask
a missing wheel/sdist file. Use the built artifact by direct path; do not install the project again
from the index during verification.

## Work Package 4 — Performance and Coverage Gates

### Fixed release performance profile

Add deterministic performance tests for package-owned database and event-delivery work only. Do
not benchmark provider response time, network latency, model generation, package installation, or
process startup because those are not controlled by this library.

Use Ubuntu, Python 3.11, the CI PostgreSQL service, and file-backed SQLite. Seed data through bulk
fixture setup outside the timed region, perform five warmups, then measure 30 samples with
`time.perf_counter_ns()`. Use fixed payload sizes and the existing public/coarse persistence paths.
The first stable release gates these reference-profile budgets:

| Operation | Fixed dataset | Gate |
| --- | --- | --- |
| Accept an idempotent submit command | Existing conversation, no provider delivery | p95 ≤ 250 ms |
| List one conversation-shell page | 10,000 owner-scoped conversations, page size 50 | p95 ≤ 250 ms |
| Search one conversation page | 10,000 indexed owner-scoped documents, two terms, page size 50 | p95 ≤ 500 ms |
| Replay committed events | 5,000 events totaling no more than 5 MiB | p95 ≤ 2 s |
| Deliver a committed event to a connected SSE consumer | One local producer/consumer | p95 ≤ 250 ms PostgreSQL; ≤ 500 ms SQLite |

Each test first asserts result correctness, owner isolation, order, and cap behavior; a fast wrong
query is not a pass. Record the query count alongside timings and fail on an N+1 increase even when
the wall-clock budget happens to pass. Keep these as release reference budgets, not universal
deployment SLAs; document the dataset and runner profile in `docs/performance.md`.

If a target fails, optimize the existing query/index/batching path and rerun parity tests. Do not
add caches, denormalized projections, settings, background indexes, or API variants unless the
measured failure proves the current Phase 10 gate cannot be met and the change stays within the
existing contract.

### Aggregate coverage

Add one dedicated Ubuntu coverage job that runs all non-live unit, property, contract, transcript,
integration, and end-to-end tests with statement coverage for `talktoharnesses`. Set
`--cov-fail-under=91`, satisfying the roadmap's “over 90%” requirement rather than rounding 90.0
upward.

Exclude generated Django migration files only. Do not omit low-coverage provider, runtime,
platform, API, or recovery modules and do not add trivial tests solely to move the percentage.
Operating-system, PostgreSQL, and live-provider jobs prove behavior but do not each collect or
enforce a second coverage number.

## Work Package 5 — CI, Build, and Publishing

### CI topology

Refactor `.github/workflows/ci.yml` so every rule has one owning job:

- `static`: locked sync/lock check, Ruff, format check, strict Pyright, migration drift, strict
  compatibility-data validation, and `render_supported --check`.
- `coverage`: the complete non-live SQLite suite and 91% aggregate coverage gate.
- `providers`: shared adapter contracts plus all provider fixture/schema/normalizer tests on Linux.
- `postgres`: PostgreSQL persistence, search, fencing/failover, recovery, interaction, and HTTP/SSE
  database-specific tests.
- `runtime-os`: Linux/macOS/Windows process supervision, path ownership, descendant cleanup,
  shutdown, and process-bound adapter transport tests only.
- `performance`: the fixed SQLite/PostgreSQL reference-profile gates.
- `build`: after the preceding jobs pass, run `uv build --no-sources`, execute artifact-content and
  isolated core/Django/all/sdist install tests, and upload the wheel and sdist as one immutable CI
  artifact.

Every dependency sync uses `--locked`. Pin the uv version used by CI/release setup rather than
silently changing build tooling between runs. Keep shared command definitions in one script or
reusable workflow invoked by both ordinary CI and the release workflow; do not copy the full gate
into two YAML files.

The direct support-document check remains named even though a unit test also covers renderer
behavior: the direct check diagnoses a stale generated file before running the suite. The generated
document itself still has one source—the packaged compatibility data.

### Manual live-harness workflow

Add a manually dispatched live workflow with one explicitly configured job per provider. Use
protected GitHub environments or appropriately isolated configured runners. The workflow does not
download unpinned provider binaries, discover executables, or persist credentials. Each job:

- checks out the exact candidate commit;
- syncs the matching locked extra;
- enables exactly one provider's live flag;
- runs that provider's create/resume/interaction gate; and
- reports the detected fixed release ID/platform without uploading native transcripts.

Because support claims depend on external credentials and executables, matrix JSON changes remain
a reviewed commit made after the corresponding run succeeds. The release checklist records links
to those runs. A missing live environment blocks the claim and therefore the stable release; it is
not converted into a skipped required check.

### Stable version transition

Phase 10 lands the stable-validation CLI, release checklist, and
`scripts/ci/stable_cut_checklist.sh` while remaining on `2026.8.0.dev9`. Populating
live-proven matrices, bumping to `2026.8.0`, tagging `v2026.8.0`, and publishing are
deferred to [Phase 12](phase12.md). Do not let the tag workflow edit or commit a
version. A tag on a development-version commit must fail when Phase 12 runs it.

### Build and publish workflow

Add a tag-triggered workflow for `v*` with least-privilege permissions:

1. Validate the tag is exactly `v` plus the stable PEP 440 project version and that it matches all
   package/compatibility/generated-document version sources.
2. Run the reusable stable release gate against the tagged checkout with the locked environment.
3. Build once with `uv build --no-sources` and run the distribution-content and isolated-install
   tests against that wheel and sdist.
4. Upload those exact artifacts from the build job and download them in a separate publish job.
5. Protect the publish job with the `pypi` GitHub environment, grant `id-token: write` only to that
   job, and use PyPI trusted publishing with `uv publish`. Store no long-lived PyPI token.
6. Publish only the downloaded `dist/*` files. Never rebuild after approval and never publish from
   a mutable branch or manual local command as part of the documented release path.

This follows uv's recommendation to build publishable artifacts with `uv build --no-sources` and
supports credential-free trusted publishing from GitHub Actions:
[uv package guide](https://docs.astral.sh/uv/guides/package/) and
[uv GitHub Actions guide](https://docs.astral.sh/uv/guides/integration/github/).

## Tests and Final Phase Gate

### Compatibility and public-contract tests

- Parameterize the shared matrix validation over all five strict JSON documents.
- Verify create/resume/platform membership is enforced before any native request is made.
- Verify development matrices may be empty but stable validation rejects any empty adapter matrix.
- Verify JSON order does not affect deterministic Markdown order and regeneration has no diff.
- Verify all compatibility adapter versions match installed metadata at the stable gate.
- Snapshot the reviewed public `__all__` names, import them from a clean interpreter, and prove
  implementation-only convenience exports are absent.
- Re-run all strict unknown-version, unknown-field, optional-dependency, and Django-free import
  tests after the audit.

### Documentation and operations tests

- Execute the README/Django setup snippets in a minimal host test project rather than checking code
  blocks as strings.
- Run Django system checks and migration drift for SQLite and PostgreSQL using the documented
  settings/extras.
- Exercise lifespan startup, generic readiness, graceful shutdown, and cleanup command using the
  documented commands.
- Review the deployment, upgrade, live-testing, performance, and release documents against the
  final routes, settings, error behavior, compatibility data, and workflow names.
- Scan all documents and workflow fixtures for tokens, absolute developer paths, native transcript
  payloads, or credential examples that look usable.

### Distribution and workflow tests

- Inspect wheel/sdist contents and metadata, then perform isolated core, Django-only, `all`, and
  sdist installs.
- Verify a release check rejects a mismatched tag, `.devN` version, local version, stale support
  document, empty matrix, mismatched adapter version, or rebuilt/different artifact.
- Verify the publish job has no repository write permission or static package-index credential and
  receives artifacts only from its successful build dependency.
- Run the 91% coverage and fixed performance gates in their dedicated jobs.

### Definition-of-done journeys

Run the same product journey once through direct `TalkToHarnessesService` calls and once through
the authenticated HTTP/SSE API. Use deterministic fake adapters for the release workflow; the five
separate live gates establish native compatibility.

For each journey:

1. Create two owners and prove the second cannot observe the first owner's harness, conversation,
   interaction, search result, or event stream.
2. Create/probe a harness, create a conversation, subscribe to events, submit an idempotent turn,
   and observe ordered committed deltas and an authoritative terminal state.
3. Exercise a durable interaction resolution and persistent approval rule, proving publication
   precedes provider delivery and duplicate answers do not win.
4. Reconnect after a saved event sequence and prove replay plus live delivery has no committed gap
   or duplicate.
5. Interrupt another turn, queue/edit/cancel a prompt, switch harnesses through the candidate path,
   and verify search/detail projections remain one canonical conversation.
6. Restart the service, recover or lazily resume according to durable state, then shut down within
   the shared ten-second budget with no owned runtime resources left.

### Final acceptance sequence

Phase 10 acceptance is machinery-complete on `2026.8.0.dev9`:

1. Exact matrix schema/enforcement, renderer, and development/stable validation modes are present.
2. All five live create/resume/interaction modules exist and fail closed when enabled without
   prerequisites; matrices may remain empty.
3. Public-export audit, packaging/install matrix, operator docs, performance budgets, split CI,
   manual live workflow, and tag/publish workflow are present and green for development validation.
4. Fake-adapter definition-of-done journeys pass from a built wheel.
5. `bash scripts/ci/stable_cut_checklist.sh` correctly reports that stable publication is still
   blocked while matrices are empty.

The publication acceptance sequence (live-run links, 91% coverage, populated matrices, `v2026.8.0`,
trusted publish, and post-publish index smoke) is owned by [Phase 12](phase12.md).

The Phase 10 gate passes when release-readiness machinery is merged on `2026.8.0.dev9`: exact
matrix schema and enforcement, live create/resume/interaction modules for all five adapters,
documented public/operational contracts, packaging/install matrix, split CI with performance and
coverage fail-under configuration, and the tag/publish workflow. Empty matrices and sub-91%
coverage remain valid Phase 10 exit conditions only while the package is still `.devN`; closing
them for publication is [Phase 12](phase12.md).

## Implementation Order

1. Merge and verify Phase 9. Inventory release gaps without changing the development version.
2. Add the shared exact matrix-entry schema, centralized validation/enforcement, renderer update,
   and structural compatibility tests.
3. Complete Grok and all five live create/resume/interaction gates. Resolve real adapter defects in
   their existing seams; do not publish matrix rows yet if evidence is incomplete.
4. Audit public exports and artifact contents, then add isolated core/Django/all/sdist install
   tests.
5. Write and execute README, deployment, upgrade, live-testing, performance, and release
   documentation against the development build.
6. Split CI into focused static, coverage, provider, database, OS-runtime, performance, and build
   jobs; add the manual live workflow and tag/publish workflow.
7. Hand off live evidence, coverage closeout, stable metadata cut, tag, and publication to
   [Phase 12](phase12.md).

## Explicitly Out of Scope

- New harnesses, provider capabilities, protocol versions, compatibility ranges, native-version
  discovery/install/update, private SDK patches, or automatic fallbacks.
- New API routes, facade methods, synchronous wrappers, CLI commands, public recovery controls,
  authentication flows, rate limiting, or administrative surfaces.
- Any ORM migration, new projection/index, caching layer, external event broker, scheduler, or
  configurable retention/recovery/performance policy.
- Phase 11 search ranking/query syntax, configurable retention, public transcript/handoff features,
  plugins, dynamic adapter discovery, and additional projections.
- Container images, Helm charts, Terraform, systemd units, reverse-proxy configuration, dashboards,
  alerts, or a bundled OpenTelemetry SDK/exporter/collector. Phase 10 documents integration points
  only.
- Backward/zero-downtime mixed-version migration guarantees, automated database restore, or
  provider-side deletion of abandoned native sessions.
- SBOM/signing/provenance systems, TestPyPI promotion, changelog automation, or GitHub Release
  generation. Add them only under a separate explicit requirement.

## Assumptions

- `2026.8.0` is the approved first stable CalVer; Phase 12 owns cutting and publishing it. PyPI is
  the release registry.
- The PyPI project and GitHub `pypi` environment are configured for trusted publishing outside the
  repository before Phase 12 publication. Missing external configuration blocks publication but
  does not justify a token-based fallback in the workflow.
- Phase 12 assumes at least one securely configured runner/platform for each provider's exact live
  create/resume gate. The initial stable matrix need not claim all three operating systems, but it
  must not claim an untested one.
- Provider credentials and workspace permissions are equivalent between create and resume runs.
  Live tests use disposable repositories and accept provider cost.
- The detailed Phase 9 OpenTelemetry dependency boundary supersedes the early roadmap summary's
  `otel` extra. Hosts that want telemetry install/configure their own SDK and exporters.
- Performance numbers are regression gates for the documented CI reference profile, not latency
  guarantees for arbitrary hardware, provider networks, database topology, or workload mix.
- The first stable upgrade procedure is stop/migrate/start. Future backward or rolling-upgrade
  guarantees require their own schema and deployment requirements.

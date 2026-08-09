# Phase 12 — First Stable Publication

## Summary

- Start Phase 12 only after Phase 10 release-readiness machinery is merged on
  `2026.8.0.dev9`. Keep that development version until every Phase 12 gate passes;
  there is no `2026.8.0.dev12` milestone.
- Phase 12 owns the work that still blocks publishing `2026.8.0`: live create/resume
  evidence, exact matrix population, closing the aggregate coverage gate, resolving
  any remaining adapter contract defects that prevent honest support claims, the
  stable version cut, and trusted publication of the exact verified artifacts.
- Do not add product behavior, provider capabilities, persistence concepts,
  migrations, or Phase 11 search/retention/transcript work.
- Do not weaken compatibility, approval, or coverage contracts to force the
  milestone. Private SDK attributes, auto-approval, inferred platforms, and
  trivial coverage padding remain forbidden.

## Current Baseline and Entry Conditions

Phase 10 has already delivered the release-readiness *machinery* on `2026.8.0.dev9`:

- Shared `CompatibilityMatrixEntry` (`release_id`, `platform`) with centralized
  validation/membership and adapter create/resume enforcement.
- Opt-in live create/resume/interaction modules for all five providers, including
  Grok. Matrices remain empty until live evidence exists.
- Trimmed public `__all__` surfaces, packaging/install matrix, operator docs, split
  CI, performance budgets, manual live workflow, and tag/publish workflow.
- Stable validation mode, `scripts/ci/stable_cut_checklist.sh`, and fake-adapter
  definition-of-done journeys.

The following release blockers remain and are **Phase 12 work**, not Phase 10
exceptions:

1. **Live evidence** — no published create/resume matrix rows yet; every matrix is
   still empty. Support claims require passing live workflow runs for each exact
   `(release_id, platform)` tuple.
2. **Coverage gate** — CI enforces `--cov-fail-under=91` with migrations omitted,
   but the non-live suite is still below that threshold (~82% at Phase 10 merge).
   Raise coverage only with meaningful path tests.
3. **Codex approval contract** — the packaged Codex release notes still record that
   public `AsyncCodex` lacks a broker-compatible deferred approval handler. Codex
   create/resume rows stay empty until a release can pass the live interaction gate
   without private SDK attributes or silent auto-approval.
4. **Stable metadata cut and publish** — package/adapter versions, generated
   `SUPPORTED_HARNESSES.md`, tag `v2026.8.0`, and PyPI trusted publishing of the
   exact verified wheel/sdist bytes.

External configuration that Phase 12 consumes but does not invent in-repo:

- Protected live harness environments / runners with credentials and executables.
- GitHub `pypi` environment configured for trusted publishing.

## Release Invariants

These rules remain the source of truth; Phase 12 executes them rather than
redefining them:

1. A create or resume support claim exists only as a matrix entry containing that
   release ID and one exact platform exercised by a live gate on the same package
   revision and native version tuple.
2. Each stable adapter has at least one published create entry and one published
   resume entry. A resume entry references a release whose capability record
   advertises resume.
3. Create support never implies resume support, and support on one operating system
   never implies another.
4. Empty matrices remain valid only while the distribution version is `.devN`.
   Stable validation rejects empty matrices and development versions.
5. `pyproject.toml` remains the distribution-version source. Installed
   `__version__`, all five adapter versions, artifact filenames, the tag, and the
   generated support document must equal `2026.8.0` at publish time.
6. Live credentials, executable paths, prompts containing secrets, tokens, and
   provider output are never committed, uploaded as workflow artifacts, or printed
   by release checks.
7. Phase 12 introduces no ORM schema change. Upgrade documentation continues to use
   the existing forward migrations through Phase 9.

## Work Package 1 — Live Evidence and Exact Matrices

1. Configure protected live environments for Grok, Cursor, Codex, Claude Code, and
   OpenCode. Missing environments block the corresponding claim; they are not
   converted into skipped required checks.
2. Run each provider's create/resume/interaction gate from
   [docs/live-testing.md](live-testing.md) / `.github/workflows/live.yml` on the
   candidate commit.
3. After a gate passes on a platform, add only that exact
   `{release_id, platform}` create and resume row to the packaged JSON document.
   Do not list untested platforms.
4. If Codex (or any other adapter) cannot pass the broker-compatible approval path
   required by advertised capabilities, fix the defect in its existing seam or keep
   its matrix empty and leave the stable release blocked for that adapter.
5. Regenerate `SUPPORTED_HARNESSES.md` and confirm
   `python -m talktoharnesses.providers.render_supported --check` is clean.
6. Confirm `bash scripts/ci/stable_cut_checklist.sh` reports non-empty create and
   resume matrices for all five adapters.

## Work Package 2 — Aggregate Coverage to 91%

1. Keep the dedicated Ubuntu coverage job with `--cov-fail-under=91` and omit only
   generated Django migrations.
2. Close the gap from the Phase 10 baseline (~82%) to 91% by adding meaningful
   unit/contract/integration coverage for real missed paths — especially
   persistence, runtime manager, worker coordinator, and provider adapter/normalizer
   branches that production code exercises.
3. Do not omit low-coverage provider, runtime, platform, API, or recovery modules.
4. Do not add trivial tests solely to move the percentage.
5. OS, PostgreSQL, and live-provider jobs continue to prove behavior without each
   collecting a second coverage number.

## Work Package 3 — Stable Version Cut and Publication

After Work Packages 1 and 2 are green on `2026.8.0.dev9`:

1. Follow [docs/releasing.md](releasing.md).
2. `uv version 2026.8.0` and set each compatibility document's `adapter_version` to
   `2026.8.0`.
3. Remove provisional “implementation target only” notes that no longer describe
   published rows.
4. Run stable compatibility validation
   (`python -m talktoharnesses.providers.render_supported --validate stable --check`).
5. Run the full CI topology, artifact install matrix, performance gates, and both
   definition-of-done journeys from the built wheel.
6. Merge the stable-version commit without further code changes, then create the
   exact tag `v2026.8.0` on that commit.
7. Let the tag-triggered release workflow validate the tag, run the stable gate,
   build once, and publish only the downloaded `dist/*` artifacts through the
   protected `pypi` environment with `uv publish` / trusted publishing.
8. In a fresh environment, install `talktoharnesses==2026.8.0` with core,
   Django-only, and `all`, and repeat import/metadata smoke checks.

A tag on a development-version commit must fail. The publish job must never rebuild
after approval.

## Tests and Final Phase Gate

- Parameterized live create/resume/interaction success for every published matrix
  row, with links recorded in the release checklist.
- Coverage job ≥ 91% on the non-live suite.
- Stable validation rejects empty matrices, mismatched adapter versions, stale
  generated docs, and `.devN` / local tags.
- Packaging and isolated install matrix pass against the exact release artifacts.
- Definition-of-done journeys pass from the built wheel (service facade and
  authenticated HTTP/SSE) using deterministic fake adapters.
- Post-publish smoke installs from the index succeed for core, Django-only, and
  `all`.

The Phase 12 gate passes only when all five adapters have exact live-tested create
and resume claims, aggregate coverage is over 90%, every release gate is green, and
the tagged artifacts are the exact verified bytes published to PyPI as
`2026.8.0`.

## Implementation Order

1. Confirm Phase 10 machinery is merged and still green on `2026.8.0.dev9`.
2. Close the coverage gap to 91% with meaningful tests; keep the fail-under gate.
3. Configure live environments and run all five live gates; fix real adapter defects
   in existing seams.
4. Populate only proven matrix rows; regenerate `SUPPORTED_HARNESSES.md`; run
   `stable_cut_checklist.sh`.
5. Cut metadata to `2026.8.0`, run the stable gate and built-wheel journeys, tag
   `v2026.8.0`, and publish the verified artifacts.

## Explicitly Out of Scope

- Phase 11 search ranking/query syntax, configurable retention, public
  transcript/handoff features, plugins, dynamic adapter discovery, and additional
  projections.
- New harnesses, compatibility ranges, native-version discovery/install/update,
  private SDK patches, or automatic fallbacks.
- New API routes, facade methods, CLI commands, authentication flows, or
  administrative surfaces.
- Any ORM migration, caching layer, external event broker, or package-owned
  OpenTelemetry SDK/exporter.
- Container images, Helm charts, Terraform, SBOM/signing/provenance systems,
  TestPyPI promotion, changelog automation, or GitHub Release generation unless
  added under a separate explicit requirement.

## Assumptions

- `2026.8.0` remains the approved first stable CalVer and PyPI remains the release
  registry.
- Phase 11 product work begins only after Phase 12 has released and verified
  `2026.8.0`.
- At least one securely configured runner/platform is available for each provider's
  exact live create/resume gate. The initial stable matrix need not claim all three
  operating systems, but it must not claim an untested one.
- Provider credentials and workspace permissions are equivalent between create and
  resume runs. Live tests use disposable workspaces and accept provider cost.
- Missing external live or PyPI configuration blocks publication; it does not
  justify a token-based publish fallback or fabricated matrix rows.

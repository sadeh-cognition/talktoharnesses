# Release checklist

Ordered checklist for publishing `talktoharnesses`. Owned by
[Phase 12](phase12.md) after Phase 10 lands the release-readiness machinery.
Contains no credentials and no mutable patch allowlist — floors and last-verified
identities live in packaged compatibility JSON and
[`SUPPORTED_HARNESSES.md`](../SUPPORTED_HARNESSES.md).

Phase 11 product work already landed on `2026.8.1.dev1`. The first stable
publication from this tree is therefore **`2026.8.1`** / tag **`v2026.8.1`**
(not `2026.8.0`).

## Preconditions

1. Phase 10 release-readiness machinery is merged; Phase 9 recovery/fencing/
   secret-sink/readiness/shutdown gates remain green.
2. Links to passing live create/resume/interaction workflow runs exist for the
   packaged floor (and optional `latest_verified`) on the candidate commit.
3. Package version is still `2026.8.1.dev1` until the Phase 12 stable cut commit.
4. Aggregate statement coverage for `talktoharnesses` is at least 91% on the
   non-live suite (migrations omitted only). Raise coverage with meaningful
   path tests — do not add trivial assertions solely to move the percentage.
5. `bash scripts/ci/stable_cut_checklist.sh` reports a floor and platform for
   every adapter.

## Candidate commit gate

1. Static checks: lockfile, Ruff, format, strict Pyright, migration drift,
   development compatibility validation, `render_supported --check`.
2. Coverage job ≥ 91% on the non-live suite.
3. Provider, PostgreSQL, OS-runtime, and performance jobs green.
4. Build job produces one wheel and one sdist; artifact content and isolated
   core / Django / `all` / sdist install tests pass.
5. Public-surface contract and packaging tests pass.
6. Definition-of-done journeys pass from the built wheel (service facade and
   authenticated HTTP/SSE) using deterministic fake adapters.

## Stable version cut

1. Confirm floors, platforms, and optional `latest_verified` identities;
   regenerate `SUPPORTED_HARNESSES.md`.
2. `uv version 2026.8.1` and set each compatibility document's `adapter_version`
   to `2026.8.1`.
3. Remove provisional “implementation target only” notes that no longer describe
   published rows.
4. Run stable compatibility validation
   (`python -m talktoharnesses.providers.render_supported --validate stable --check`).
5. Merge the stable-version commit with no further code changes.
6. Tag exact `v2026.8.1` on that commit. A tag on a development version must fail.

## Publish

1. Tag-triggered workflow validates tag == `v` + project version and runs the
   reusable stable gate.
2. Build once with `uv build --no-sources`; run distribution tests against those
   exact artifacts.
3. Upload artifacts from the build job; download them in the publish job.
4. Publish only downloaded `dist/*` with PyPI trusted publishing (`uv publish`)
   from the protected `pypi` environment. Never rebuild after approval.
5. In a fresh environment, install `talktoharnesses==2026.8.1` with core,
   Django-only, and `all`, and repeat import/metadata smoke checks.

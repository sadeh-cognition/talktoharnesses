#!/usr/bin/env bash
# Shared CI helpers for ordinary CI and the release workflow.
set -euo pipefail

UV_VERSION="${UV_VERSION:-0.12.3}"
PYTHON_VERSION="${PYTHON_VERSION:-3.11}"

ci_setup() {
  local extras="${1:-django}"
  # shellcheck disable=SC2086
  uv sync --locked ${extras}
}

ci_static() {
  uv lock --check
  uv run ruff check .
  uv run ruff format --check .
  uv run pyright
  uv run pytest tests/test_migration_drift.py -q --tb=short
  uv run python -m talktoharnesses.providers.render_supported --validate development --check
}

ci_coverage() {
  # Live suites are opt-in. Performance has its own CI gate and its repeated
  # warmups/samples should not delay the coverage gate.
  uv run pytest \
    -n auto \
    --maxprocesses=4 \
    --dist=worksteal \
    --ignore=tests/live \
    --ignore=tests/performance \
    --cov=talktoharnesses \
    --cov-report=term-missing \
    "$@" \
    -q --tb=short
}

ci_providers() {
  uv run pytest \
    tests/contract \
    tests/unit/providers \
    -q --tb=short
}

ci_postgres() {
  uv run pytest \
    tests/test_django_persistence.py \
    tests/test_phase8_persistence.py \
    tests/test_phase8_fts.py \
    tests/e2e/test_phase6_approvals_gate.py \
    tests/e2e/test_phase9_recovery_gate.py \
    tests/unit/django/test_approval_api.py \
    tests/unit/application/test_interaction_broker.py \
    tests/unit/application/test_command_processor_interactions.py \
    tests/unit/application/test_fencing.py \
    tests/unit/application/test_worker_coordinator.py \
    tests/unit/domain/test_interaction_answers.py \
    tests/unit/domain/test_approval_matching.py \
    tests/unit/providers/grok/test_permission_fixtures.py \
    -q --tb=short
}

ci_runtime_os() {
  uv run pytest tests/runtime -q --tb=short
}

ci_performance() {
  uv run pytest -n 0 tests/performance -q --tb=short
}

ci_build() {
  rm -rf dist
  uv build --no-sources
  uv run pytest tests/test_packaging.py -q --tb=short
}

ci_stable_gate() {
  uv lock --check
  uv run ruff check .
  uv run ruff format --check .
  uv run pyright
  uv run pytest tests/test_migration_drift.py -q --tb=short
  uv run python -m talktoharnesses.providers.render_supported --validate stable --check
  ci_coverage --cov-fail-under=91
  ci_providers
  ci_performance
  ci_build
}

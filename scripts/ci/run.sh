#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck source=scripts/ci/common.sh
source "${ROOT}/scripts/ci/common.sh"

usage() {
  echo "usage: $0 {static|coverage|providers|postgres|runtime-os|performance|build|stable-gate}" >&2
  exit 2
}

[[ $# -eq 1 ]] || usage
case "$1" in
  static) ci_static ;;
  coverage) ci_coverage ;;
  providers) ci_providers ;;
  postgres) ci_postgres ;;
  runtime-os) ci_runtime_os ;;
  performance) ci_performance ;;
  build) ci_build ;;
  stable-gate) ci_stable_gate ;;
  *) usage ;;
esac

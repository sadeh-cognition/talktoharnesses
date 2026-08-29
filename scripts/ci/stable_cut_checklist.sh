#!/usr/bin/env bash
# Preconditions for the final 2026.8.1 cut. Exits non-zero until live evidence
# and stable metadata are ready. Does not bump versions or publish.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck source=scripts/ci/common.sh
source "${ROOT}/scripts/ci/common.sh"

cd "${ROOT}"
uv sync --locked --extra django --extra all >/dev/null

VERSION="$(uv version --short)"
echo "package_version=${VERSION}"

if [[ "${VERSION}" == *dev* ]]; then
  echo "still on development version; confirm floors, then uv version 2026.8.1" >&2
fi

uv run python scripts/render_supported.py --check

echo "floors present; run: bash scripts/ci/run.sh stable-gate"

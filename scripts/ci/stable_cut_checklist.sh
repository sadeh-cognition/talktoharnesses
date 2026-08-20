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

uv run python - <<'PY'
from talktoharnesses.providers.claude.compatibility import load_claude_compatibility
from talktoharnesses.providers.codex.compatibility import load_codex_compatibility
from talktoharnesses.providers.cursor.compatibility import load_cursor_compatibility
from talktoharnesses.providers.grok.compatibility import load_grok_compatibility
from talktoharnesses.providers.opencode.compatibility import load_opencode_compatibility
from talktoharnesses.providers.prime_agent.compatibility import load_prime_agent_compatibility

docs = {
    "grok": load_grok_compatibility(),
    "cursor": load_cursor_compatibility(),
    "codex": load_codex_compatibility(),
    "claude": load_claude_compatibility(),
    "opencode": load_opencode_compatibility(),
    "prime_agent": load_prime_agent_compatibility(),
}
ready = True
for name, doc in docs.items():
    latest = doc.latest_verified.identity if doc.latest_verified is not None else None
    print(
        f"{name}: floor={doc.floor.version} platforms={list(doc.floor.platforms)} "
        f"latest_verified={latest} adapter_version={doc.adapter_version}"
    )
    if not doc.floor.version or not doc.floor.platforms:
        ready = False
if not ready:
    raise SystemExit(
        "stable cut blocked: every adapter needs a compatibility floor and platform"
    )
PY

echo "floors present; run: bash scripts/ci/run.sh stable-gate"

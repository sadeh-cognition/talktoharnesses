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
  echo "still on development version; populate live matrix rows, then uv version 2026.8.1" >&2
fi

uv run python - <<'PY'
from talktoharnesses.providers.claude.compatibility import load_claude_compatibility
from talktoharnesses.providers.codex.compatibility import load_codex_compatibility
from talktoharnesses.providers.cursor.compatibility import load_cursor_compatibility
from talktoharnesses.providers.grok.compatibility import load_grok_compatibility
from talktoharnesses.providers.opencode.compatibility import load_opencode_compatibility

docs = {
    "grok": load_grok_compatibility(),
    "cursor": load_cursor_compatibility(),
    "codex": load_codex_compatibility(),
    "claude": load_claude_compatibility(),
    "opencode": load_opencode_compatibility(),
}
ready = True
for name, doc in docs.items():
    create_n = len(doc.create_matrix)
    resume_n = len(doc.resume_matrix)
    print(f"{name}: create={create_n} resume={resume_n} adapter_version={doc.adapter_version}")
    if create_n == 0 or resume_n == 0:
        ready = False
if not ready:
    raise SystemExit(
        "stable cut blocked: every adapter needs at least one live-proven "
        "create and resume matrix row"
    )
PY

echo "matrix rows present; run: bash scripts/ci/run.sh stable-gate"

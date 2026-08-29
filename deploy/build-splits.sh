#!/usr/bin/env bash
# Build every top-level tth-<kind> split service image.
#
# Usage: deploy/build-splits.sh [tag] [kind ...]
#   tag defaults to "latest"; kinds default to all six.
#
# UID/GID build args match the invoking user so the executable-ownership
# check inside the container accepts the installed CLIs, and files written
# into the bind-mounted projects dir stay owned by the host user.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TAG="${1:-latest}"
shift || true
KINDS=("$@")
if [ ${#KINDS[@]} -eq 0 ]; then
    KINDS=(grok cursor codex claude opencode prime-agent)
fi

for kind in "${KINDS[@]}"; do
    repo="${REPO_ROOT}/tth-${kind}"
    if [ ! -f "${repo}/Dockerfile" ]; then
        echo "skipping ${kind}: ${repo}/Dockerfile not found" >&2
        continue
    fi
    echo "=== building tth-${kind}:${TAG} ==="
    docker buildx build \
        --build-context tth_types="${REPO_ROOT}/tth-types" \
        --build-arg UID="$(id -u)" \
        --build-arg GID="$(id -g)" \
        --load \
        -t "tth-${kind}:${TAG}" \
        "${repo}"
done

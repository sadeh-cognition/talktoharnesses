#!/usr/bin/env bash
# Build the top-level tth-<kind> split service images, in parallel, via bake.
#
# Usage: deploy/build-splits.sh [tag] [kind ...]
#   tag defaults to "latest"; kinds default to all seven (see ../docker-bake.hcl).
#
# UID/GID build args match the invoking user so the executable-ownership
# check inside the container accepts the installed CLIs, and files written
# into the bind-mounted projects dir stay owned by the host user.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TAG="${1:-latest}"
shift || true

# bake only reads build contexts below the working directory without extra
# --allow flags, so run it from the repo root whatever directory invoked us.
cd "${REPO_ROOT}"
TAG="${TAG}" HOST_UID="$(id -u)" HOST_GID="$(id -g)" \
    docker buildx bake -f docker-bake.hcl "$@"

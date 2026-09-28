---
type: decision
title: Sandbox Toolchain Hygiene
status: implemented
audiences:
  - developer
tags:
  - type/decision
  - audience/developer
last_verified: 2026-09-28
verified_against_commit: e908d9fb7d76d46fd6f56d4ea60087f3e64ab846
sources:
  - raw/product/sandbox-workspace-provisioning.md
  - raw/product/sandbox-image-customization-amendment.md
---

# Sandbox Toolchain Hygiene

## Context

The split images built the service venv at `/opt/venv`, owned by the service user, and exported `UV_PROJECT_ENVIRONMENT=/opt/venv`, `UV_PYTHON`, `UV_PYTHON_DOWNLOADS=never` and a `PATH` starting with `/opt/venv/bin` image-wide. Harness children inherited that environment, so an agent's `uv run --python 3.13` in a mounted project replaced the service venv and broke `/v1/health`. Fresh git worktrees also had no way to get their dependencies installed short of the agent doing it ad hoc.

## Decision

- The service runtime is root-owned at `/opt/tth/venv`, off `PATH`, and named only by the image `CMD` and `HEALTHCHECK`. The uv settings that build it are scoped to the two `uv sync` `RUN` lines, which use a throwaway uv cache (`UV_NO_CACHE=1`) rather than a BuildKit cache mount: policy image instructions build on the same BuildKit, and a cache mount is shared state any build that names it can write, so a harness image must read nothing a policy build can leave behind. Agents see plain `uv` with its dev-box defaults. This is safe for the SDK-managed kinds because neither Claude nor Codex runs the executable-ownership check (their `_KIND_EXECUTABLES` is empty).
- Toolchain caches are managed container environment injected by `SandboxManager`, not image `ENV`, so they take part in container drift detection and cannot be overridden by operator passthrough. Downloads and caches live on the per-kind `/data` volume.
- Every image carries Node 22 with `npm` and `corepack`, independent of what the harness needs; Node 22 is pinned because it is the last line that ships corepack.
- Splits seal `TTH_SPLIT_TOKEN` and `DJANGO_SETTINGS_MODULE` out of `os.environ` after Django is configured. Stripping in the vendored supervisor alone would not cover the SDK-managed kinds, whose SDKs inherit `os.environ` wholesale.
- Workspace setup is repo-declared (`.tth/setup.sh`), never detected and never an API field, and its failure fails the turn. Since 2026-09-28 it covers workspace dependencies only.
- System packages, compilers and other image-level tools come from Dockerfile instructions in the project's sandbox policy, the only way to customize a sandbox image. TTH parses them and builds, on the kind's harness image, a Dockerfile it writes from the parsed instructions (every instruction on one line, `RUN` as a JSON exec form with `<` escaped), never the text as written, so BuildKit cannot read an option, instruction or heredoc the parser did not. It appends a trailer, read from that image, that restores `WORKDIR`, `HOME`, `DJANGO_SETTINGS_MODULE` and `USER`. A build that changes the split's user, command, health check, ports, volumes or shell, exports a new `UV_*` variable, or is not layered on the harness image is discarded. See [Customize sandbox images](../requirements/customize-sandbox-images.md).
- The setup runner lives in the proxy and is shipped into the container as source over `docker exec`. Putting it in the splits would have vendored it seven times; a one-shot container would have lost the session's mounts and limits.
- The stamp includes the post-run manifests (an `npm install` writes `package-lock.json`) so the next session skips.

## Consequences

- Existing containers are recreated once on first use after upgrade, which drops in-flight sessions; schedule the rollout when conversations are idle.
- Images grow by Node (about 330 MB).
- With no shared uv cache, a split image rebuild after a `uv.lock` change downloads the locked dependencies again (about 12 seconds for `tth-codex` on 2026-09-28); a source-only rebuild is unaffected.
- A workspace setup shares the container's `pids_limit` and `mem_limit` with running sessions.
- Cross-kind runs in one directory are not serialized; scripts must stay idempotent.
- A policy with image instructions builds its image at the first preparation after the instructions or the harness image change, so that session waits for the build. Each combination of text, kind and harness image is a separate image until the scope reaper removes it. Image instructions must install to system paths: `/home/agent` and `/data` are named volumes, and image content there reaches only a new, empty volume.

## Related

- [Provision sandbox workspaces](../requirements/provision-sandbox-workspaces.md)
- [Customize sandbox images](../requirements/customize-sandbox-images.md)
- [Split services decision](split-services.md)
- [Isolated harness runtimes](../capabilities/isolated-harness-runtimes.md)
- [Approved sandbox workspace provisioning](../../raw/product/sandbox-workspace-provisioning.md)
- [Approved sandbox image customization](../../raw/product/sandbox-image-customization-amendment.md)

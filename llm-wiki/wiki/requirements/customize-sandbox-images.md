---
type: requirement
title: Customize sandbox images
status: implemented
audiences:
  - product
  - developer
tags:
  - type/requirement
  - capability/runtime
  - status/implemented
last_verified: 2026-09-28
verified_against_commit: e908d9fb7d76d46fd6f56d4ea60087f3e64ab846
sources:
  - raw/product/sandbox-image-customization-amendment.md
  - raw/product/project-sandbox-policies.md
  - raw/product/sandbox-workspace-provisioning.md
---

# Customize sandbox images

## Intent

A project adds system packages, compilers and other image-level tools to its
sandboxes through its sandbox policy, without changing the harness images
every other project runs. Dockerfile instructions in the policy are the only
way to customize sandbox images; `.tth/setup.sh` stays for workspace
dependencies. An image built from a policy follows its harness image, is never
used once that harness image has been rebuilt, and is removed automatically
when nothing needs it. See the
[approved amendment](../../raw/product/sandbox-image-customization-amendment.md).

## Current behavior

`SandboxPolicy` has an optional `image_dockerfile` field of at most 16,384
characters, stored with each immutable policy revision. Validation normalizes
line endings to LF and strips surrounding whitespace; blank text becomes
`None`. A policy without instructions serializes without the field, both in
stored revisions and in API responses, so readers built before the field
existed (`extra="forbid"`) still accept it. Rows saved before the field
existed still validate.

`tth_types.image_instructions.parse_image_instructions` parses the text into
instructions and rejects what a harness image must not run. It follows
Docker's rules for comments, line continuations (blank and comment lines
inside one are skipped and the pieces are joined without a separator) and
heredocs (a whole shell word `<<NAME`, `<<-NAME` or a quoted name, with an
optional file descriptor on `RUN`). It allows only `RUN`, `ENV`, `ARG`,
`USER`, `WORKDIR`, `COPY`, `ADD` and `LABEL`, rejects `FROM` with its own
message and every other instruction by name, and rejects any option on `RUN`
(`--mount`, `--network`, `--security`, `--device`, ...) and on `ENV`, `ARG`,
`USER`, `WORKDIR` and `LABEL`. `COPY` and `ADD` take only their harmless
options (`--from`, `--chown`, `--chmod`, `--link`, `--parents`, `--exclude`;
`--checksum`, `--keep-git-dir`, `--unpack` for `ADD`) as plain `--name=value`
tokens, and a `COPY` or `ADD` with a heredoc takes only plain paths and
options, without quotes, backslashes or `$`. Control characters, keywords that
are not ASCII, and text ending inside a continuation or an open heredoc are
rejected.

The text is never built as written. `custom_images.render` writes the parsed
instructions again: each on one line; `RUN` as a JSON exec form in the base
image's shell (default `/bin/sh -c`) with `<` escaped as `\u003c`, its heredoc
bodies folded into the shell command, and `RUN <<EOF` run as the body itself
(as an executable when it starts with `#!`); `COPY` and `ADD` without
heredocs as JSON arrays; a `COPY` or `ADD` heredoc as written; everything else
as `KEYWORD arguments`. BuildKit therefore finds no option, instruction or
heredoc the parser did not, whatever the original text looked like. The
rendered file is `FROM <base tag>`, those instructions, then a trailer read
from the base image's configuration that restores `WORKDIR`, `HOME`,
`DJANGO_SETTINGS_MODULE` and `USER` (Docker's `/` and `root` when the base
leaves them empty), followed by the `tth.image=derived`, `tth.derived`,
`tth.state`, `tth.kind`, `tth.base-id`, `tth.dockerfile-sha256` and
`tth.contract` labels.

Preparation ensures the kind's base image first, as before. When the policy
has instructions, `IsolatedSandbox._ensure_image` writes the scope's
`image.json` (kind, base image tag, text digest, contract version) and builds
the derived image before the scope lock is taken; under the lock,
`_split_image` enters `custom_images.ensure_derived_image` again and finds it
built. The derived image is tagged `tth-<kind>-custom:<hash>`, where the hash
covers the contract version (2), a digest of the state root, the base image id
and the text digest, so identical text shares one image across policies,
revisions and base tags of one image within a state root, and a rebuilt base
changes the tag. A per-tag lock under the state root's `.locks/images` is
held from the image lookup until the split container exists; a lock taken on
a lock file that garbage collection deleted meanwhile is dropped and taken
again. A missing image, or one whose `tth.*` labels do not match, is built
with `docker buildx build --builder <context> --load --progress=plain -t <tag> -`,
where `<context>` is the current Docker context (`docker context show`),
whose builder is the daemon's own and resolves `FROM` against the local base
image whatever buildx builder is selected. The rendered Dockerfile arrives on
stdin with no build context, the build has the host's normal network access,
it is bounded by the sandbox build timeout, and its output is decoded as UTF-8
with replacement. When the base tag names a different image after the build
(the base was rebuilt meanwhile), the image is discarded and built once more
on the new base; a second change fails preparation.

Every build is verified against the base image: the user, working directory
(empty and Docker's default compare equal), entrypoint, command, health
check, exposed ports, volumes, stop signal, shell and on-build triggers must
match; `HOME`, `VIRTUAL_ENV` and every variable starting with `UV_`,
`PYTHON`, `DJANGO_`, `TALKTOHARNESSES_` or `TTH_` must keep the base image's
value or stay unset; `PATH` must keep the base image's directories in order;
the base image's layers must be the derived image's first layers; and the
labels must be present. A failed build or verification removes the image and
raises `sandbox_unavailable` with reason `custom_image_build_failed`. The
public message names the policy's image instructions and points to the server
logs; build output stays in the proxy log. There is no fallback to the base
image.

Derived builds share the host's BuildKit with the harness and gateway image
builds, so those builds mount no BuildKit cache: `scripts/render_dockerfiles.py`
builds the service venv with a throwaway uv cache (`UV_NO_CACHE=1`), and the
gateway Dockerfile mounts nothing. A cache mount a policy build writes cannot
reach a harness or gateway image.

The auth and RTK seeders and the split container all run the derived image,
and the sandbox row records it. Container reuse compares image tags, so a
container on the base image, on an older derived image, or on an image built
on a replaced base is recreated. The gateway configuration excludes
`image_dockerfile`, so gateway images built before the field existed keep
accepting it. Background readiness never builds.

Each [scope reaper](reclaim-sandbox-scopes.md) pass creates one Docker client;
when Docker is unreachable it logs one warning and skips both reaping and
image cleanup. After reclaiming scopes it calls
`ScopedSandboxManager.collect_images`, which runs
`custom_images.collect_garbage`. That removes derived images labelled with
this state root's `tth.state`, tagged or not, that no container of any state
uses and that no scope under the state root wants on the base image its
`image.json` names, and untagged images labelled `tth.image=base` or
`tth.image=gateway` (leftovers of a rebuild) that no container uses. Derived
images of other state roots are never touched. Removal never forces, skips an
image whose lock a preparation holds, skips Docker's in-use conflicts, and
deletes the lock file of every derived image that no longer exists.
`docker-bake.hcl` labels the base images `tth.image=base` and the gateway
`tth.image=gateway`; the proxy's fallback gateway build adds the same label.

## Gap

Instructions are built lazily: the first session after the instructions
change or the base image is rebuilt waits for the build, which the retryable
`sandbox_preparing` error covers up to the build timeout. A broken build is
retried at every preparation, and its output is visible only in the proxy log.
Builds run as root with the host's network, so whoever may edit a project's
policy decides what runs in its sandbox image, and `COPY --from` can read any
image on the Docker host. The parser follows Docker's rules for ordinary text
but is not Docker's parser; where they would differ, the canonical rendering
decides what is built, so unusual text can build differently from how Docker
would read it. Tightening the parser later can make stored revisions fail to
load, since revisions are re-validated on read. Derived images use disk per
text, kind, base image and state root, and BuildKit's build cache is never
pruned. Images inherit labels, so an untagged operator image built `FROM` a
harness image is removed like a rebuild leftover. Harness and gateway images
left untagged by rebuilds before the `tth.image` label existed, derived images
built before contract 2, and lock files named by the earlier scheme are not
recognized. The live gate builds and cleans up derived images with real
Docker but does not run a harness session in one; that path is covered by
unit tests.

## Acceptance criteria

- A policy without image instructions runs the harness images unchanged.
- A policy's instructions are applied to the harness image of every kind its
  sandboxes run, and its split and seeder containers run the result.
- Instructions containing `FROM`, or instructions that would change how the
  split starts, listens or mounts, are rejected when the policy is saved.
- An image built on an older harness image is never used; the next preparation
  builds a new one.
- A build that fails or breaks the split contract fails preparation with
  `custom_image_build_failed` and never falls back to the base image.
- Derived images nothing uses or wants, and base and gateway images left
  untagged by a rebuild, are removed automatically.
- `.tth/setup.sh` keeps providing workspace dependencies.

## Implementation evidence

- `tth-types/src/tth_types/image_instructions.py`: `IMAGE_INSTRUCTIONS`,
  `ImageInstruction`, `Heredoc`, `parse_image_instructions`
- `tth-types/src/tth_types/sandbox.py`: `SandboxPolicy.image_dockerfile`,
  `IMAGE_DOCKERFILE_MAX_CHARS`, `permitted_dockerfile`, `omit_unset_image`
- `tth-types/src/tth_types/errors.py`: the `custom_image_build_failed` reason
- `src/talktoharnesses/remote/custom_images.py`: `state_id`, `derived_tag`,
  `image_record`, `render`, `image_lock`, `ensure_derived_image`,
  `collect_garbage`, `_contract_problem`
- `src/talktoharnesses/remote/isolated_sandbox.py`: `_ensure_image`,
  `_split_image`, `_container_image`, the gateway configuration's exclusion,
  the gateway fallback build's label
- `src/talktoharnesses/remote/sandbox.py`: the sandbox row records
  `_container_image`
- `src/talktoharnesses/remote/docker_ops.py`: `base_image`,
  `image_environment`, `docker_driver_builder`, `run_image_build` with
  `stdin`, `reason` and replacement decoding
- `src/talktoharnesses/remote/scope_layout.py`: `ScopeLayout.image_file`
- `src/talktoharnesses/remote/scoped_sandboxes.py`: `collect_images`
- `src/talktoharnesses/remote/scope_reaper.py`: `ScopeReaper.tick`,
  `ReapReport.images`
- `docker-bake.hcl`: `tth.image` labels
- `scripts/render_dockerfiles.py`: `_SERVICE_UV_ENV` and `_uv_sync` build the
  service venv without a BuildKit cache mount
- `deploy/README.md`: Custom sandbox images

The recorded commit is the inspected baseline; this page describes the
associated uncommitted worktree changes.

## Test evidence

- `tests/unit/remote/test_image_instructions.py`: accepted and rejected
  instructions, including the texts that once got `RUN --mount` past the
  earlier scanner, continuations and heredocs read like Docker's, and a
  policy without instructions serializing without the field
- `tests/unit/remote/test_custom_images.py`: tag inputs, the canonical
  rendering, rendered RUN scripts run by `/bin/sh` behaving like Docker's
  heredocs, defaults for a base without user or working directory, a build by
  the daemon's builder from stdin holding the lock, reuse across base tags of
  one image, rebuilds on mismatched labels and on a base rebuilt during the
  build, every verification failure discarding the image with the public
  message, every cleanup rule (used, wanted, recorded base, other state
  roots, superseded, older contract, untagged base and gateway images, locked,
  in use, already gone, lock files), a waiter on a deleted lock file, output
  that is not UTF-8, and the builder name
- `tests/unit/remote/test_isolated_sandbox.py`
  (`test_launch_keeps_secrets_and_public_network_outside_agent[custom_image]`,
  `test_derived_image_is_built_before_the_scope_lock_is_taken`): seeders and
  the split run the derived image, `image.json` is written, the build happens
  before the scope lock, and the gateway configuration omits the field and
  validates as `GatewayConfig`
- `tests/unit/remote/test_scope_reaper.py`: image cleanup runs after reaping
  and against the fake Docker without failing, a cleanup failure keeps the
  reaping report, an unreachable Docker skips reaping and cleanup with one
  warning, and a disabled reaper removes no images
- `tests/unit/remote/test_readiness_sandbox.py`: the Django policy store keeps
  the text and readiness never prepares
- `tests/test_render_dockerfiles.py`
  (`test_images_read_no_build_cache_a_policy_build_can_write`): no split or
  gateway Dockerfile uses `--mount`, and the split venv is built with
  `UV_NO_CACHE=1`
- `tests/live/test_custom_sandbox_image_live.py`
  (`TALKTOHARNESSES_SANDBOX_DOCKER=1`): with real Docker and `tth-codex`,
  instructions with a `COPY` heredoc, a `#!` `RUN` heredoc and `<<` in a
  command build, the derived image runs as `agent` in `/app/service` with
  their results, a rebuilt base produces a new tag, and cleanup under the
  test's own state root removes the superseded image while keeping the
  current one

The proxy suite passes 1070 tests outside `tests/live` with 92.16% coverage;
`custom_images.py` is 99% covered. The live test passes. Lint, pyright, split
drift and Dockerfile rendering pass; the format check reports only
`src/talktoharnesses/gateway/server.py`, unformatted on the baseline.

## Related

- [Project sandbox policies](project-sandbox-policies.md)
- [Provision sandbox workspaces](provision-sandbox-workspaces.md)
- [Reclaim sandbox scopes](reclaim-sandbox-scopes.md)
- [Sandbox toolchain hygiene decision](../decisions/sandbox-toolchain-hygiene.md)
- [Isolated harness runtimes](../capabilities/isolated-harness-runtimes.md)
- [Deployment](../operations/deployment.md)
- [Approved sandbox image customization](../../raw/product/sandbox-image-customization-amendment.md)

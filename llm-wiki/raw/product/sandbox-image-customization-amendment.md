# Sandbox image customization amendment

Approved: 2026-09-28

Amends the project sandbox policy request and the sandbox workspace
provisioning requirements: a project's sandbox policy can customize the images
its sandboxes run, and that is the only way to customize sandbox images.

Motivation: a benchmark project needed C++ and Rust compilers in its Codex
sandbox. They were first added to the shared `tth-codex` image, which gave
them to every project. The user asked for a per-project mechanism instead.

## Decisions

- A sandbox policy may carry Dockerfile instructions, without `FROM`. TTH
  applies them to the harness image of every kind that policy's sandboxes run
  and runs that policy's sandboxes from the resulting image. A policy without
  instructions runs the harness images unchanged.
- The instructions are one more editable policy field, stored with each
  immutable policy revision, with the same permission as the other policy
  fields.
- There is no separate package-list field, and no other image customization
  mechanism. Toolchains added to one project do not go into the shared images.
- `.tth/setup.sh` stays, but only for workspace dependencies (for example
  `uv sync` or `npm ci` in a mounted worktree), which an image cannot provide
  because worktrees are mounted when the container starts.
- An image built from a policy's instructions follows its harness image: when
  the harness image is rebuilt, the next sandbox preparation builds a new one,
  and an image built on an older harness image is never used.
- Old images are cleaned up automatically: images built from policy
  instructions that nothing uses or needs any more, and harness and gateway
  images left behind by a rebuild.

## Supersession

This source narrows `raw/product/sandbox-workspace-provisioning.md`. Its
statement that a repository's `.tth/setup.sh` is the single source of setup
now covers workspace dependencies only; system packages, compilers and other
image-level tools come from the policy's image instructions. Its toolchain
statement (uv downloads the Python a project pins; Node 22 with npm and
corepack ships in every image; caches live under `/data`) remains in force.
That source remains preserved as approved product input.

This source extends `raw/product/project-sandbox-policies.md`: the editable
project policy gains the image instructions. Its egress, credential,
publication and command-check statements are unchanged.

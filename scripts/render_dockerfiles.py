#!/usr/bin/env python3
"""Render every tth-<kind>/Dockerfile (and .dockerignore) from one template.

The split images share everything except the harness CLI they embed. Keeping the
common instructions byte-identical, in the same order, lets BuildKit reuse the base
layers across all seven images in one bake run, and a ``--check`` run in the static
gate stops the copies drifting apart the way the vendored modules once did.

Usage: ``python scripts/render_dockerfiles.py [--check]``. Exit status 1 when a
rendered file is out of date (``--check``).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from splits import KINDS, ROOT, package_name, split_root

# Blocks are written at column 0 and joined with blank lines, so each one is a
# self-contained Dockerfile fragment without leading or trailing blank lines.

RTK_INSTALL = """\
# RTK (https://github.com/rtk-ai/rtk) rewrites shell commands to `rtk <cmd>` so
# the harness sees a token-trimmed version of the output. Pinned release binary.
ARG RTK_VERSION=0.49.0
RUN set -eu; case "$(uname -m)" in \\
      x86_64)  rtk_target=x86_64-unknown-linux-musl ;; \\
      aarch64) rtk_target=aarch64-unknown-linux-gnu ;; \\
      *) echo "unsupported architecture for rtk: $(uname -m)" >&2; exit 1 ;; \\
    esac; \\
    rtk_url="https://github.com/rtk-ai/rtk/releases/download/v${RTK_VERSION}"; \\
    curl -fsSL "${rtk_url}/rtk-${rtk_target}.tar.gz" | tar -xz -C /usr/local/bin rtk \\
    && chmod 0755 /usr/local/bin/rtk \\
    && rtk --version"""

CURSOR_INSTALL = """\
# Pinned versioned artifact (the cursor.com/install script always installs
# latest, which can run ahead of the adapter's verified matrix).
ARG CURSOR_VERSION=2026.08.11-e8db854
ARG CURSOR_DOWNLOADS=https://downloads.cursor.com/lab
RUN mkdir -p /opt/harness/cursor-agent \\
    && curl -fSL "${CURSOR_DOWNLOADS}/${CURSOR_VERSION}/linux/x64/agent-cli-package.tar.gz" \\
       | tar --strip-components=1 -xzf - -C /opt/harness/cursor-agent \\
    && chown -R ${UID}:${GID} /opt/harness \\
    && chmod -R a+rX /opt/harness
ENV TALKTOHARNESSES_CURSOR_EXECUTABLE=/opt/harness/cursor-agent/cursor-agent"""

OPENCODE_INSTALL = """\
ARG OPENCODE_VERSION=1.18.19
RUN npm install -g opencode-ai@${OPENCODE_VERSION} \\
    && chown -R ${UID}:${GID} /usr/lib/node_modules/opencode-ai"""

GROK_INSTALL = """\
# Installer writes under $HOME; runtime $HOME is the credentials volume, so
# install with HOME=/opt/harness. TTH refuses executables not owned by the
# effective UID, so the install is chowned to the service user.
ARG GROK_INSTALL_URL=https://x.ai/cli/install.sh
RUN mkdir -p /opt/harness \\
    && HOME=/opt/harness bash -c "curl -fsSL ${GROK_INSTALL_URL} | bash" \\
    && chown -R ${UID}:${GID} /opt/harness \\
    && chmod -R a+rX /opt/harness
ENV TALKTOHARNESSES_GROK_EXECUTABLE=/opt/harness/.grok/bin/grok"""

PRIME_AGENT_INSTALL = """\
# prime-agent is not published to the npm registry; releases ship as tarballs.
ARG PRIME_AGENT_VERSION=0.7.1
ARG PRIME_AGENT_RELEASES=https://pub-728493de92a943e2a9b2d17b4719f318.r2.dev/releases
RUN npm install -g \\
      "${PRIME_AGENT_RELEASES}/v${PRIME_AGENT_VERSION}/prime-agent-${PRIME_AGENT_VERSION}.tgz" \\
    && chown -R ${UID}:${GID} /usr/lib/node_modules/prime-agent"""

# Runs as the service user (the installer writes under $HOME).
MUSE_INSTALL = """\
ENV PATH=/home/agent/.local/bin:$PATH \\
    MUSE_NO_AUTO_UPDATE=1
RUN curl -fsSL https://dev.meta.ai/install.sh -o /tmp/install-muse.sh \\
    && bash /tmp/install-muse.sh \\
    && muse --version"""

DOCKERIGNORE = """\
.venv/
.git/
.pytest_cache/
.ruff_cache/
__pycache__/
*.pyc
.coverage
dist/
tests/
Dockerfile
.dockerignore
"""


@dataclass(frozen=True)
class Split:
    kind: str  # directory suffix, e.g. "prime-agent"
    node: bool = False
    extra_apt: tuple[str, ...] = ()
    root_installs: tuple[str, ...] = ()  # before USER agent
    agent_installs: tuple[str, ...] = ()  # as the service user


SPLITS: dict[str, Split] = {
    split.kind: split
    for split in (
        Split("grok", node=True, root_installs=(GROK_INSTALL,)),
        Split(
            "cursor",
            node=True,
            # Pyright's nodeenv Node needs libatomic.so.1 (not in slim).
            extra_apt=("libatomic1", "make", "ripgrep"),
            root_installs=(CURSOR_INSTALL, RTK_INSTALL),
        ),
        Split("codex", root_installs=(RTK_INSTALL,)),
        Split("claude", root_installs=(RTK_INSTALL,)),
        Split("opencode", node=True, root_installs=(OPENCODE_INSTALL, RTK_INSTALL)),
        Split("prime-agent", node=True, root_installs=(PRIME_AGENT_INSTALL,)),
        Split("muse", agent_installs=(MUSE_INSTALL,)),
    )
}
assert tuple(SPLITS) == KINDS


def _run(steps: list[str]) -> str:
    return "RUN " + " \\\n    && ".join(steps)


def render_dockerfile(split: Split) -> str:
    package = package_name(split.kind)
    apt = ["apt-get update"]
    apt.append(
        "apt-get install -y --no-install-recommends "
        + " ".join(("git", "curl", "ca-certificates", *split.extra_apt))
    )
    if split.node:
        apt.append("curl -fsSL https://deb.nodesource.com/setup_${NODE_MAJOR}.x | bash -")
        apt.append("apt-get install -y --no-install-recommends nodejs")
    apt.append("rm -rf /var/lib/apt/lists/*")
    command = ["python", "-m", "uvicorn", f"{package}.asgi:application"]
    command += ["--host", "0.0.0.0", "--port", "8010"]

    blocks = [
        f"""\
# syntax=docker/dockerfile:1
# GENERATED by scripts/render_dockerfiles.py -- edit the template there, not here.
# Split service image for the {split.kind} harness. Build from the repo root with
#   docker buildx bake {split.kind}
# (docker-bake.hcl supplies the tth_types build context and the host UID/GID).

ARG PYTHON_VERSION=3.12
ARG UV_VERSION=0.12.3

FROM ghcr.io/astral-sh/uv:${{UV_VERSION}} AS uv

FROM python:${{PYTHON_VERSION}}-slim-bookworm

# Match the host user so agent-created files in mounted worktrees are owned
# by (and committable for) the host-side workflow worker.
ARG UID=1000
ARG GID=1000""",
    ]
    if split.node:
        blocks.append("ARG NODE_MAJOR=22")
    # Everything from here to the DJANGO_SETTINGS_MODULE line is shared between
    # the splits that take the same branch (node or not, extra apt packages, harness
    # installer). BuildKit keys RUN layers on their environment, so nothing per-kind
    # may be set above that line or the shared layers stop being shared.
    blocks += [
        "ENV PYTHONUNBUFFERED=1",
        _run(apt),
        _run(
            [
                "groupadd --gid ${GID} agent",
                "useradd --uid ${UID} --gid ${GID} --create-home --shell /bin/bash agent",
                "mkdir -p /data /opt/venv",
                "chown ${UID}:${GID} /data /opt/venv",
            ]
        ),
        _run(
            [
                'git config --system user.name "Agentbahn Sandbox Agent"',
                'git config --system user.email "agent-sandbox@localhost"',
                'git config --system safe.directory "*"',
            ]
        ),
        """\
# Python packages go into a venv owned by the service user: the SDK-managed
# harnesses ship their CLI inside site-packages and the executable-ownership
# check needs it owned by the effective UID (a chown -R afterwards would
# duplicate the whole layer).
COPY --from=uv /uv /bin/uv
ENV PATH=/opt/venv/bin:$PATH \\
    UV_PROJECT_ENVIRONMENT=/opt/venv \\
    UV_PYTHON=/usr/local/bin/python3 \\
    UV_PYTHON_DOWNLOADS=never \\
    UV_LINK_MODE=copy""",
        *split.root_installs,
        "USER agent\nENV HOME=/home/agent",
        *split.agent_installs,
        f"""\
# Per-kind from here on. Layers are ordered by change frequency: locked
# third-party dependencies, then the shared schemas, then the service source.
ENV DJANGO_SETTINGS_MODULE={package}.settings

# uv.lock pins tth-types as ../tth-types relative to the project directory.
WORKDIR /app/service
COPY pyproject.toml README.md uv.lock ./
RUN --mount=type=cache,target=/home/agent/.cache/uv,uid=${{UID}},gid=${{GID}} \\
    uv sync --frozen --no-dev --no-editable \\
      --no-install-project --no-install-package tth-types
COPY --from=tth_types pyproject.toml README.md uv.lock ../tth-types/
COPY --from=tth_types src ../tth-types/src
COPY src ./src
RUN --mount=type=cache,target=/home/agent/.cache/uv,uid=${{UID}},gid=${{GID}} \\
    uv sync --frozen --no-dev --no-editable

EXPOSE 8010
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s \\
    CMD curl -fsS http://127.0.0.1:8010/v1/health || exit 1
CMD {json.dumps(command)}""",
    ]
    return "\n\n".join(blocks) + "\n"


def rendered_files() -> dict[Path, str]:
    files = {ROOT / "tth-types" / ".dockerignore": DOCKERIGNORE}
    for split in SPLITS.values():
        files[split_root(split.kind) / "Dockerfile"] = render_dockerfile(split)
        files[split_root(split.kind) / ".dockerignore"] = DOCKERIGNORE
    return files


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail if any rendered file is stale")
    args = parser.parse_args()
    stale: list[str] = []
    for path, rendered in rendered_files().items():
        current = path.read_text() if path.exists() else None
        if rendered == current:
            continue
        if args.check:
            stale.append(str(path.relative_to(ROOT)))
        else:
            path.write_text(rendered)
            print(f"rendered {path.relative_to(ROOT)}")
    if stale:
        print("split Dockerfiles are out of date; run scripts/render_dockerfiles.py to regenerate:")
        print("\n".join(f"  {path}" for path in stale))
        return 1
    if args.check:
        print("split Dockerfiles are up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())

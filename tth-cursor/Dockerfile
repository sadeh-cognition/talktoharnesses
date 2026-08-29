# syntax=docker/dockerfile:1
# Split service image for the cursor harness. Build with a named context for
# the shared schema package (see talktoharnesses/deploy/build-splits.sh):
#   docker buildx build --build-context tth_types=./tth-types -t tth-cursor ./tth-cursor

ARG PYTHON_VERSION=3.12

FROM python:${PYTHON_VERSION}-slim-bookworm

# Match the host user so agent-created files in mounted worktrees are owned
# by (and committable for) the host-side workflow worker.
ARG UID=1000
ARG GID=1000
ARG NODE_MAJOR=22

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DJANGO_SETTINGS_MODULE=tth_cursor.settings

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git curl ca-certificates \
        # Pyright's nodeenv Node needs libatomic.so.1 (not in slim).
        libatomic1 \
        make \
        ripgrep \
    && curl -fsSL https://deb.nodesource.com/setup_${NODE_MAJOR}.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid ${GID} agent \
    && useradd --uid ${UID} --gid ${GID} --create-home --shell /bin/bash agent \
    && mkdir -p /data \
    && chown ${UID}:${GID} /data

RUN git config --system user.name "Agentbahn Sandbox Agent" \
    && git config --system user.email "agent-sandbox@localhost" \
    && git config --system safe.directory "*"

WORKDIR /app
COPY --from=tth_types . ./tth-types
COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install ./tth-types .

# Pinned versioned artifact (the cursor.com/install script always installs
# latest, which can run ahead of the adapter's verified matrix).
ARG CURSOR_VERSION=2026.08.11-e8db854
RUN mkdir -p /opt/harness/cursor-agent \
    && curl -fSL "https://downloads.cursor.com/lab/${CURSOR_VERSION}/linux/x64/agent-cli-package.tar.gz" \
       | tar --strip-components=1 -xzf - -C /opt/harness/cursor-agent \
    && chown -R ${UID}:${GID} /opt/harness \
    && chmod -R a+rX /opt/harness
ENV TALKTOHARNESSES_CURSOR_EXECUTABLE=/opt/harness/cursor-agent/cursor-agent

USER agent
ENV HOME=/home/agent
EXPOSE 8010
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s \
    CMD curl -fsS http://127.0.0.1:8010/v1/health || exit 1
CMD ["python", "-m", "uvicorn", "tth_cursor.asgi:application", "--host", "0.0.0.0", "--port", "8010"]

# syntax=docker/dockerfile:1
# Split service image for the grok harness. Build with a named context for
# the shared schema package (see talktoharnesses/deploy/build-splits.sh):
#   docker buildx build --build-context tth_types=./tth-types -t tth-grok ./tth-grok

ARG PYTHON_VERSION=3.12

FROM python:${PYTHON_VERSION}-slim-bookworm

# Match the host user so agent-created files in mounted worktrees are owned
# by (and committable for) the host-side workflow worker.
ARG UID=1000
ARG GID=1000
ARG NODE_MAJOR=22

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DJANGO_SETTINGS_MODULE=tth_grok.settings

RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl ca-certificates \
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

# Installer writes under $HOME; runtime $HOME is the credentials volume, so
# install with HOME=/opt/harness. TTH refuses executables not owned by the
# effective UID, so the install is chowned to the service user.
ARG GROK_INSTALL_URL=https://x.ai/cli/install.sh
RUN mkdir -p /opt/harness \
    && HOME=/opt/harness bash -c "curl -fsSL ${GROK_INSTALL_URL} | bash" \
    && chown -R ${UID}:${GID} /opt/harness \
    && chmod -R a+rX /opt/harness
ENV TALKTOHARNESSES_GROK_EXECUTABLE=/opt/harness/.grok/bin/grok

USER agent
ENV HOME=/home/agent
EXPOSE 8010
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s \
    CMD curl -fsS http://127.0.0.1:8010/v1/health || exit 1
CMD ["python", "-m", "uvicorn", "tth_grok.asgi:application", "--host", "0.0.0.0", "--port", "8010"]

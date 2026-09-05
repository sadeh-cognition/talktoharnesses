# syntax=docker/dockerfile:1
# Split service image for the muse harness. Build with a named context for
# the shared schema package (see talktoharnesses/deploy/build-splits.sh):
#   docker buildx build --build-context tth_types=./tth-types -t tth-muse ./tth-muse

ARG PYTHON_VERSION=3.12

FROM python:${PYTHON_VERSION}-slim-bookworm

# Match the host user so agent-created files in mounted worktrees are owned
# by (and committable for) the host-side workflow worker.
ARG UID=1000
ARG GID=1000

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DJANGO_SETTINGS_MODULE=tth_muse.settings

RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl ca-certificates \
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

USER agent
ENV HOME=/home/agent \
    PATH=/home/agent/.local/bin:$PATH \
    MUSE_NO_AUTO_UPDATE=1
RUN curl -fsSL https://dev.meta.ai/install.sh -o /tmp/install-muse.sh \
    && bash /tmp/install-muse.sh \
    && muse --version
EXPOSE 8010
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s \
    CMD curl -fsS http://127.0.0.1:8010/v1/health || exit 1
CMD ["python", "-m", "uvicorn", "tth_muse.asgi:application", "--host", "0.0.0.0", "--port", "8010"]

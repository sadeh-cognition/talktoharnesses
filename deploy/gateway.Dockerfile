FROM ghcr.io/astral-sh/uv:0.12.3 AS uv
FROM python:3.12-slim-bookworm
COPY --from=uv /uv /usr/local/bin/uv
ARG UID=1000
ARG GID=1000
RUN groupadd --gid ${GID} gateway && useradd --uid ${UID} --gid ${GID} --create-home gateway
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY tth-types ./tth-types
COPY src ./src
RUN uv sync --frozen --no-dev --extra gateway
USER gateway
ENV PATH=/app/.venv/bin:$PATH
ENTRYPOINT ["python", "-m", "talktoharnesses.gateway.server", "/state/config.json"]

# syntax=docker/dockerfile:1.7

ARG PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.29@sha256:eb2843a1e56fd9e30c7276ce1a52cba86e64c7b385f5e3279a0e08e02dd058fc

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app

COPY --from=uv /uv /uvx /usr/local/bin/
COPY pyproject.toml uv.lock ./

# Keep dependency installation cacheable when only application code changes.
RUN uv sync --locked --no-dev --no-install-project

COPY README.md ./
COPY src/ ./src/
RUN uv sync --locked --no-dev --no-editable

FROM ${PYTHON_IMAGE} AS runtime

ARG APP_UID=10001
ARG APP_GID=10001

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1

LABEL org.opencontainers.image.title="Events Concierge" \
      org.opencontainers.image.description="Events Concierge API and durable workers"

RUN groupadd --gid "${APP_GID}" events-concierge \
    && useradd \
        --uid "${APP_UID}" \
        --gid "${APP_GID}" \
        --create-home \
        --home-dir /home/events-concierge \
        --shell /usr/sbin/nologin \
        events-concierge \
    && mkdir -p /app /var/lib/events-concierge/claim-check \
    && chown -R events-concierge:events-concierge \
        /app /var/lib/events-concierge /home/events-concierge

WORKDIR /app
COPY --from=builder --chown=events-concierge:events-concierge /app/.venv /app/.venv
COPY --chown=events-concierge:events-concierge alembic.ini ./
COPY --chown=events-concierge:events-concierge migrations/ ./migrations/

USER events-concierge
EXPOSE 8000
STOPSIGNAL SIGTERM

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/readyz', timeout=2).read()"]

CMD ["uvicorn", "events_concierge.api.app:app", "--host", "0.0.0.0", "--port", "8000", "--no-server-header", "--no-access-log"]

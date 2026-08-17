# Multi-stage build for the chatbot-api service (CLAUDE.md packaging
# standard: "Docker multi-stage, non-root, healthchecks"). One image, three
# process roles in production (api / celery-worker / celery-beat) -- the
# role is selected entirely by the container's `command:` in
# deploy/docker-compose.prod.yml, never by building three separate images.
#
# services/common is a local sibling package, never published to an index
# ("chatbot-common" in services/api/pyproject.toml resolves against
# whatever is already installed, not PyPI) -- it MUST be installed before
# services/api, and this Dockerfile MUST be built with the repo root as
# its context so both directories are visible to COPY:
#
#   docker build -f services/api/Dockerfile -t chatbot-api:<tag> .
#
# (note the trailing "." -- context is the repo root, not services/api/).
#
# Multi-arch by construction: python:3.12-slim ships amd64 and arm64
# variants, and build-essential in the builder stage covers any dependency
# without a prebuilt arm64 wheel yet -- this same image builds cleanly on
# an Oracle Cloud "Always Free" ARM VM (deploy runbook Tier C) with no
# changes.

FROM python:3.12-slim AS builder
WORKDIR /build

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY services/common ./services/common
COPY services/api ./services/api

RUN python -m venv /venv
ENV PATH="/venv/bin:${PATH}"
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir ./services/common ./services/api

FROM python:3.12-slim AS runtime

# Non-root (CLAUDE.md packaging standard). No shell login, no home-dir
# writes expected -- STORAGE_LOCAL_ROOT (if used) must point somewhere
# this user can write, set explicitly in the prod .env, never assumed.
RUN groupadd --system app && useradd --system --gid app --create-home --home-dir /home/app app

WORKDIR /app
COPY --from=builder /venv /venv
ENV PATH="/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Alembic config + migration scripts are needed IN the image so
# `docker compose run --rm api alembic upgrade head` works as a one-off
# without a second build context (deploy runbook Phase 4). The installed
# package itself (site-packages, from the builder stage) is what actually
# serves traffic; this is only the migrations tooling.
COPY services/api/alembic.ini services/api/alembic.ini
COPY services/api/migrations services/api/migrations

# Pre-created (not left for the app to mkdir at runtime) so a named volume
# mounted at /app/storage (deploy/docker-compose.prod.yml, shared between
# api_1/api_2/celery-worker so an upload written by one container is
# readable by the ingestion task in another) inherits app:app ownership on
# first mount instead of Docker's default root:root for a fresh volume.
RUN mkdir -p /app/storage && chown -R app:app /app
USER app

EXPOSE 8000

# Curl isn't installed in slim -- use Python's stdlib so the healthcheck
# doesn't need an extra apt-get layer. /healthz has no dependencies (DB,
# Redis) by design (CLAUDE.md: "liveness, no deps") -- exactly what a
# container healthcheck should probe; /readyz is for the LB/deploy script,
# not this.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/healthz', timeout=3).status == 200 else 1)"

# Default role is the API server; celery-worker/celery-beat override this
# via `command:` in docker-compose.prod.yml.
CMD ["uvicorn", "api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]

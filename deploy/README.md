# Deploy — Local Development Infrastructure

This directory contains Docker Compose configuration and initialization scripts for the local development environment.

## Files

- **`docker-compose.dev.yml`** — Orchestration for Postgres (with pgvector), PgBouncer (transaction pooler), and Redis. All services include healthchecks and use sensible defaults via environment variable substitution (`${VAR:-default}`).
- **`postgres/init/01-extensions.sql`** — Enables pgvector extension in the main `chatbot` database.
- **`postgres/init/02-test-db.sh`** — Creates the `chatbot_test` database and enables pgvector in it (for integration tests).

## Bring-up

From the repository root:

```bash
# Start all services in the background
docker compose -f deploy/docker-compose.dev.yml up -d

# Check status (all should be "healthy")
docker compose -f deploy/docker-compose.dev.yml ps
```

## Verification

```bash
# Check pgvector in the main database:
docker compose -f deploy/docker-compose.dev.yml exec postgres \
  psql -U chatbot -d chatbot -c "SELECT extversion FROM pg_extension WHERE extname='vector';"

# Check pgvector in the test database:
docker compose -f deploy/docker-compose.dev.yml exec postgres \
  psql -U chatbot -d chatbot_test -c "SELECT extversion FROM pg_extension WHERE extname='vector';"

# Verify Redis:
docker compose -f deploy/docker-compose.dev.yml exec redis redis-cli ping
# Expected output: PONG

# Verify PgBouncer (requires psql):
psql "postgresql://chatbot:chatbot@localhost:6432/chatbot" -c "select 1;"
# Expected output: integer column with value 1
```

## Configuration

Service defaults (all can be overridden via environment variables):

| Service     | Port (default) | User/Password (default) | Database (default) |
|-------------|----------------|-------------------------|-------------------|
| Postgres    | 5432           | `chatbot:chatbot`       | `chatbot`          |
| PgBouncer   | 6432           | (same as Postgres)      | (same as Postgres) |
| Redis       | 6379           | (no auth)               | —                  |

To customize, set environment variables before bringing up:

```bash
export POSTGRES_USER=myuser
export POSTGRES_PASSWORD=mypassword
export POSTGRES_DB=mydb
export POSTGRES_PORT=15432
export PGBOUNCER_PORT=16432
export REDIS_PORT=16379
docker compose -f deploy/docker-compose.dev.yml up -d
```

Or add a `.env` file in the repository root (see `.env.example`).

## Tear-down

```bash
# Stop and remove containers (volume persists)
docker compose -f deploy/docker-compose.dev.yml down

# Stop, remove containers, and delete volumes (full reset)
docker compose -f deploy/docker-compose.dev.yml down -v
```

## Migrations

Database migrations are managed by Alembic and live in `services/api/migrations/`. Migrations always target **Postgres directly (port 5432)**, not PgBouncer, because PgBouncer's transaction pool mode cannot safely execute DDL.

### Prerequisites

Install migration dependencies:

```bash
# From the repository root
venv/Scripts/python -m pip install -e services/api
```

### Commands

Run from the `services/api` directory:

```bash
# Upgrade to the latest migration (head)
python -m alembic upgrade head

# Show the current applied revision
python -m alembic current

# Downgrade to the baseline (no revisions applied)
python -m alembic downgrade base

# Create a new migration (after defining changes)
python -m alembic revision -m "description of changes"
```

Migrations use the **`DATABASE_URL_DIRECT`** environment variable (targets port 5432). If not set, they fall back to `DATABASE_URL`. A clear error is raised if neither is available.

### Example Round-trip (Verification)

```bash
cd services/api
# Set the environment variable (or ensure .env is loaded)
export DATABASE_URL_DIRECT='postgresql://chatbot:chatbot@localhost:5432/chatbot'

# Apply all migrations
python -m alembic upgrade head

# Verify the applied revision
python -m alembic current
# Expected output: shows revision 0001 (head)

# Downgrade to baseline (reverses all migrations)
python -m alembic downgrade base

# Reapply (clean round-trip)
python -m alembic upgrade head

# Verify in the database
docker compose -f ../deploy/docker-compose.dev.yml exec postgres \
  psql -U chatbot -d chatbot -c "\dt alembic_version"
# Expected output: shows the alembic_version table
```

## Notes

- **PgBouncer transaction pool mode:** All application code connects via PgBouncer (port 6432) for connection pooling. However, migrations and integration tests must connect directly to Postgres (port 5432) because PgBouncer's transaction pool mode cannot handle DDL. See `.env.example` for `DATABASE_URL` vs `DATABASE_URL_DIRECT`.
- **asyncpg + PgBouncer:** When using `asyncpg` through PgBouncer's transaction pool mode, `statement_cache_size=0` avoids prepared statement desynchronization -- as a `Database.connect(..., statement_cache_size=0)` keyword (see `app.py`'s bootstrap and `common/db.py`), never appended to the `DATABASE_URL` DSN string itself: asyncpg forwards unrecognized DSN query params straight through as Postgres startup parameters, and the server rejects `statement_cache_size` outright ("unsupported startup parameter"), refusing to boot.
- **Healthchecks:** All services include healthchecks. Docker Compose waits for Postgres to be healthy before starting PgBouncer; applications should not start until all services show "healthy" in `docker compose ps`.

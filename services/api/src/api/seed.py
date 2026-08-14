"""Idempotent seed script for the chatbot platform.

Usage::

    python -m api.seed

Connects via ``DATABASE_URL_DIRECT`` (fallback ``DATABASE_URL``) using asyncpg
directly (bootstrap, pre-repo). Inserts an initial tenant, a platform-admin
user, and a client-admin user idempotently (``ON CONFLICT DO NOTHING``).

Tenant identity and admin emails are **required** env vars; the script exits
immediately if any are missing.  Passwords come from optional env vars; if
unset a random password is generated and printed once so the operator can log
in after S1.2.
"""
from __future__ import annotations

import asyncio
import os
import secrets
from pathlib import Path
from uuid import uuid4

import asyncpg
from common.crypto import hash_password
from dotenv import load_dotenv

from api.admin.repository import _hash_client_key

# Load .env from the repo root regardless of CWD (mirrors migrations/env.py and
# common.settings.Settings, which load the same file for alembic and the running app).
load_dotenv(Path(__file__).resolve().parents[4] / ".env")

_REQUIRED_ENV = (
    "SEED_TENANT_NAME",
    "SEED_TENANT_SLUG",
    "SEED_PLATFORM_ADMIN_EMAIL",
    "SEED_CLIENT_ADMIN_EMAIL",
)


def _require_env(name: str) -> str:
    """Return the value of *name* from the environment, or fail fast."""
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(
            f"Required environment variable {name} is missing or empty. "
            f"Set all of: {', '.join(_REQUIRED_ENV)}"
        )
    return value


async def _seed() -> None:
    dsn = os.environ.get("DATABASE_URL_DIRECT") or os.environ.get("DATABASE_URL")
    if not dsn:
        raise SystemExit("Set DATABASE_URL_DIRECT or DATABASE_URL to run the seed.")

    # Validate required env vars before touching the DB.
    tenant_name = _require_env("SEED_TENANT_NAME")
    tenant_slug = _require_env("SEED_TENANT_SLUG")
    pa_email = _require_env("SEED_PLATFORM_ADMIN_EMAIL")
    ca_email = _require_env("SEED_CLIENT_ADMIN_EMAIL")

    conn: asyncpg.Connection[object] = await asyncpg.connect(dsn)
    try:
        # -- Initial tenant -------------------------------------------------------
        tenant_id = uuid4().hex
        result = await conn.execute(
            "INSERT INTO tenants (id, name, slug) VALUES ($1, $2, $3) "
            "ON CONFLICT (slug) DO NOTHING",
            tenant_id,
            tenant_name,
            tenant_slug,
        )
        tenant_inserted = result.endswith("1")

        # Resolve the actual id (may already exist)
        actual_tenant_id: str | None = await conn.fetchval(
            "SELECT id FROM tenants WHERE slug = $1", tenant_slug
        )
        assert actual_tenant_id is not None  # noqa: S101

        # -- Platform admin user --------------------------------------------------
        pa_password_env = os.environ.get("SEED_PLATFORM_ADMIN_PASSWORD")
        pa_generated = False
        if pa_password_env:
            pa_password = pa_password_env
        else:
            pa_password = secrets.token_urlsafe(16)
            pa_generated = True

        pa_hash = hash_password(pa_password)
        pa_result = await conn.execute(
            "INSERT INTO users (id, tenant_id, email, role, password_hash) "
            "SELECT $1, NULL, $2, $3, $4 "
            "WHERE NOT EXISTS (SELECT 1 FROM users WHERE lower(email) = lower($2))",
            uuid4().hex,
            pa_email,
            "PLATFORM_ADMIN",
            pa_hash,
        )
        pa_inserted = pa_result.endswith("1")

        # -- Client admin user ----------------------------------------------------
        ca_password_env = os.environ.get("SEED_CLIENT_ADMIN_PASSWORD")
        ca_generated = False
        if ca_password_env:
            ca_password = ca_password_env
        else:
            ca_password = secrets.token_urlsafe(16)
            ca_generated = True

        ca_hash = hash_password(ca_password)
        ca_result = await conn.execute(
            "INSERT INTO users (id, tenant_id, email, role, password_hash) "
            "SELECT $1, $2, $3, $4, $5 "
            "WHERE NOT EXISTS (SELECT 1 FROM users WHERE lower(email) = lower($3))",
            uuid4().hex,
            actual_tenant_id,
            ca_email,
            "CLIENT_ADMIN",
            ca_hash,
        )
        ca_inserted = ca_result.endswith("1")

        # -- Summary --------------------------------------------------------------
        print("=== Seed summary ===")
        if tenant_inserted:
            print(f"  Tenant created: slug={tenant_slug}  id={actual_tenant_id}")
        else:
            print(f"  Tenant already exists: slug={tenant_slug}  id={actual_tenant_id}")

        if pa_inserted:
            msg = f"  Platform admin created: {pa_email}"
            if pa_generated:
                msg += f"  password={pa_password}"
            print(msg)
        else:
            print(f"  Platform admin already exists: {pa_email}")

        if ca_inserted:
            msg = f"  Client admin created: {ca_email}  tenant={actual_tenant_id}"
            if ca_generated:
                msg += f"  password={ca_password}"
            print(msg)
        else:
            print(f"  Client admin already exists: {ca_email}")

        if not any([tenant_inserted, pa_inserted, ca_inserted]):
            print("  Nothing inserted (idempotent).")

        # -- Widget admission: backfill client_key_hash + allowed_origins -------
        # S12.1 (migration 0030): tenants.client_key_hash stores a SHA-256 hash,
        # never the raw key. The raw key is still printed once below (unchanged
        # UX) but only its hash is ever persisted.
        existing_key_hash: str | None = await conn.fetchval(
            "SELECT client_key_hash FROM tenants WHERE id = $1", actual_tenant_id
        )
        if existing_key_hash is None:
            client_key = f"pk_{secrets.token_urlsafe(24)}"
            await conn.execute(
                "UPDATE tenants SET client_key_hash = $1 WHERE id = $2",
                _hash_client_key(client_key),
                actual_tenant_id,
            )
        else:
            # Already seeded in a prior run -- the raw key is gone (only its
            # hash is stored), so there is nothing new to print here.
            client_key = "<already set -- raw key was only shown once>"

        origins_env = os.environ.get("SEED_TENANT_ALLOWED_ORIGINS", "").strip()
        if origins_env:
            allowed_origins = [o.strip() for o in origins_env.split(",") if o.strip()]
            if allowed_origins:
                await conn.execute(
                    "UPDATE tenants SET allowed_origins = $1 WHERE id = $2",
                    allowed_origins,
                    actual_tenant_id,
                )
        else:
            allowed_origins = []

        print(f"  client_key={client_key}")
        print(f"  allowed_origins={allowed_origins}")

    finally:
        await conn.close()


def main() -> None:
    asyncio.run(_seed())


if __name__ == "__main__":
    main()

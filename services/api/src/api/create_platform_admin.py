"""One-off script: create a new PLATFORM_ADMIN user.

Usage::

    python -m api.create_platform_admin --email you@example.com [--name "Jane Doe"]

There is no self-service UI/API for this (tenant onboarding only ever
creates CLIENT_ADMIN users, and every ``/admin/users`` route explicitly
rejects a PLATFORM_ADMIN/global caller -- see ``api.admin.users_repository``)
-- platform admins are a one-off, manually-provisioned superuser role by
design, so this script exists instead of a new persistent endpoint.

The password is read interactively via ``getpass`` (typed twice, must
match) -- never a CLI argument, never an environment variable, never
logged or printed. This is deliberate: no automation or AI agent should
ever see, generate, or handle a platform-admin credential; only the human
running this script, at their own keyboard, ever knows it.

Same DB connection convention as the other one-off scripts in this module
(``DATABASE_URL_DIRECT``, falling back to ``DATABASE_URL``), and the same
insert shape ``api.admin.repository.onboard_client`` uses for a tenant's
first CLIENT_ADMIN user -- just with ``role=PLATFORM_ADMIN`` and
``tenant_id=NULL`` (the ``users_tenant_role_chk`` constraint requires
exactly that pairing).
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import re
from uuid import uuid4

import asyncpg
from common.crypto import hash_password
from common.db import Database

_MIN_PASSWORD_LENGTH = 12  # matches AdminOnboardTenantRequest.admin_password's own min_length
_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _prompt_for_password() -> str:
    while True:
        password = getpass.getpass("New platform-admin password: ")
        if len(password) < _MIN_PASSWORD_LENGTH:
            print(f"  Password must be at least {_MIN_PASSWORD_LENGTH} characters -- try again.")
            continue
        confirm = getpass.getpass("Confirm password: ")
        if password != confirm:
            print("  Passwords did not match -- try again.")
            continue
        return password


async def _run(email: str, name: str | None) -> None:
    if not _EMAIL_PATTERN.match(email):
        raise SystemExit(f"'{email}' does not look like a valid email address.")

    dsn = os.environ.get("DATABASE_URL_DIRECT") or os.environ.get("DATABASE_URL")
    if not dsn:
        raise SystemExit("Set DATABASE_URL_DIRECT or DATABASE_URL to run this script.")

    password = _prompt_for_password()
    password_hash = hash_password(password)
    del password  # out of scope the moment it's hashed

    db = await Database.connect(dsn, min_size=1, max_size=2)
    try:
        user_id = uuid4().hex
        try:
            await db.execute(
                "INSERT INTO users (id, tenant_id, email, role, password_hash, name) "
                "VALUES ($1, NULL, $2, 'PLATFORM_ADMIN', $3, $4)",
                user_id,
                email,
                password_hash,
                name,
            )
        except asyncpg.UniqueViolationError as exc:
            raise SystemExit(f"A user with email '{email}' already exists.") from exc
    finally:
        await db.close()

    print(f"=== Created PLATFORM_ADMIN user_id={user_id} email={email} ===")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", required=True, help="Login email for the new platform admin.")
    parser.add_argument("--name", default=None, help="Display name (optional).")
    args = parser.parse_args()
    asyncio.run(_run(args.email, args.name))


if __name__ == "__main__":
    main()

"""Migration 0060: client_accounts -- the new grouping entity that lets one
client login own multiple chatbots (tenants).

Expand-only: ``users.tenant_id`` and its ``users_tenant_role_chk`` CHECK stay
in place, unused by new code, for one full deploy cycle (rolling-deploy
safety) -- a later contract migration drops them once nothing reads them.

Backfill reuses each existing tenant's own ``id`` as its new account's
``id``, so ``tenants.client_account_id = tenants.id`` and
``users.client_account_id = users.tenant_id`` for every pre-existing row --
zero behavior change for any existing client until they actively create a
second chatbot.
"""
from __future__ import annotations

from alembic import op

revision = "0060"
down_revision = "0059"
branch_labels: tuple[str, ...] = ()
depends_on: tuple[str, ...] = ()


def upgrade() -> None:
    op.execute("""
        CREATE TABLE client_accounts (
            id          text PRIMARY KEY,
            name        text NOT NULL,
            created_at  timestamptz NOT NULL DEFAULT now(),
            updated_at  timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        INSERT INTO client_accounts (id, name, created_at, updated_at)
        SELECT id, name, created_at, updated_at FROM tenants
    """)

    op.execute(
        "ALTER TABLE tenants ADD COLUMN client_account_id text REFERENCES client_accounts(id)"
    )
    op.execute("UPDATE tenants SET client_account_id = id")
    op.execute("ALTER TABLE tenants ALTER COLUMN client_account_id SET NOT NULL")
    op.execute("CREATE INDEX ix_tenants_client_account_id ON tenants (client_account_id)")

    op.execute(
        "ALTER TABLE users ADD COLUMN client_account_id text REFERENCES client_accounts(id)"
    )
    op.execute("UPDATE users SET client_account_id = tenant_id WHERE tenant_id IS NOT NULL")
    op.execute("""
        ALTER TABLE users ADD CONSTRAINT users_account_role_chk CHECK (
            (role = 'PLATFORM_ADMIN' AND client_account_id IS NULL)
            OR (role <> 'PLATFORM_ADMIN' AND client_account_id IS NOT NULL)
        )
    """)
    op.execute("CREATE INDEX ix_users_client_account_id ON users (client_account_id)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_users_client_account_id")
    op.execute("ALTER TABLE users DROP CONSTRAINT IF EXISTS users_account_role_chk")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS client_account_id")
    op.execute("DROP INDEX IF EXISTS ix_tenants_client_account_id")
    op.execute("ALTER TABLE tenants DROP COLUMN IF EXISTS client_account_id")
    op.execute("DROP TABLE IF EXISTS client_accounts")

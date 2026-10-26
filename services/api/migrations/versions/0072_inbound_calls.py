"""Migration 0072: inbound calls (Sprint A7) -- number -> tenant registry and
the per-tenant switch for answering inbound calls."""
from __future__ import annotations

from alembic import op

revision = "0072"
down_revision = "0071"
branch_labels: tuple[str, ...] = ()
depends_on: tuple[str, ...] = ()


def upgrade() -> None:
    op.execute("""
        CREATE TABLE telephony_numbers (
            number_e164 text PRIMARY KEY,
            tenant_id text NOT NULL,
            provider text NOT NULL DEFAULT 'plivo',
            active boolean NOT NULL DEFAULT true,
            created_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX ix_telephony_numbers_tenant ON telephony_numbers (tenant_id)")
    op.execute("ALTER TABLE tenant_call_agent_configs "
               "ADD COLUMN inbound_enabled boolean NOT NULL DEFAULT true")
    # Numbers already saved by A6 configs become routable.
    op.execute("INSERT INTO telephony_numbers (number_e164, tenant_id) "
               "SELECT DISTINCT ON (from_number) from_number, tenant_id "
               "FROM tenant_call_agent_configs ORDER BY from_number, updated_at "
               "ON CONFLICT DO NOTHING")


def downgrade() -> None:
    op.execute("ALTER TABLE tenant_call_agent_configs DROP COLUMN IF EXISTS inbound_enabled")
    op.execute("DROP TABLE IF EXISTS telephony_numbers")

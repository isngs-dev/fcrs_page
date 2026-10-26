"""Migration 0075: lead memory + the not-interested do-not-contact reason (Sprint A11)."""
from __future__ import annotations

from alembic import op

revision = "0075"
down_revision = "0074"
branch_labels: tuple[str, ...] = ()
depends_on: tuple[str, ...] = ()

_OLD_REASONS = ("'unsubscribe', 'stop', 'dnc_request', 'bounce', 'complaint', 'wrong_number', "
                "'manual', 'registry'")


def upgrade() -> None:
    op.execute("""
        CREATE TABLE contact_memory (
            tenant_id text NOT NULL,
            contact_id text NOT NULL,
            items jsonb NOT NULL DEFAULT '[]',
            updated_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, contact_id),
            FOREIGN KEY (tenant_id, contact_id)
                REFERENCES contacts (tenant_id, contact_id) ON DELETE CASCADE
        )
    """)
    op.execute("ALTER TABLE suppression_list DROP CONSTRAINT suppression_list_reason_check")
    op.execute("ALTER TABLE suppression_list ADD CONSTRAINT suppression_list_reason_check "
               f"CHECK (reason IN ({_OLD_REASONS}, 'not_interested'))")


def downgrade() -> None:
    op.execute("DELETE FROM suppression_list WHERE reason = 'not_interested'")
    op.execute("ALTER TABLE suppression_list DROP CONSTRAINT suppression_list_reason_check")
    op.execute("ALTER TABLE suppression_list ADD CONSTRAINT suppression_list_reason_check "
               f"CHECK (reason IN ({_OLD_REASONS}))")
    op.execute("DROP TABLE IF EXISTS contact_memory")

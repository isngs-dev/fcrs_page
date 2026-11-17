"""Migration 0079: per-client qualification questions + each contact's answers and score.

Plan 1.2/1.3 (owner, 2026-11-17). ``tenant_qualification_configs`` holds a client's
weights and questions (no row = the default roofing set). ``contact_qualifications`` holds
a contact's answers (key -> chosen label, what was heard, channel, when), the score and
band last computed, and when the one-time hot-lead alert went out.
"""
from __future__ import annotations

from alembic import op

revision = "0079"
down_revision = "0078"
branch_labels: tuple[str, ...] = ()
depends_on: tuple[str, ...] = ()


def upgrade() -> None:
    op.execute("""
        CREATE TABLE tenant_qualification_configs (
            tenant_id text PRIMARY KEY,
            weights jsonb NOT NULL,
            questions jsonb NOT NULL,
            updated_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE TABLE contact_qualifications (
            tenant_id text NOT NULL,
            contact_id text NOT NULL,
            answers jsonb NOT NULL DEFAULT '{}',
            score integer NULL CHECK (score BETWEEN 0 AND 100),
            band text NULL CHECK (band IN ('high', 'medium', 'low')),
            scored_at timestamptz NULL,
            alerted_at timestamptz NULL,
            PRIMARY KEY (tenant_id, contact_id),
            FOREIGN KEY (tenant_id, contact_id)
                REFERENCES contacts (tenant_id, contact_id) ON DELETE CASCADE
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS contact_qualifications")
    op.execute("DROP TABLE IF EXISTS tenant_qualification_configs")

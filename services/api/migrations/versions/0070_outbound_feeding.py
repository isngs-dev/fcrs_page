"""Migration 0070: outbound feeding (Sprint A5) -- CSV import previews and
"Talk to Rebecca" command drafts (nothing launches until confirmed)."""
from __future__ import annotations

from alembic import op

revision = "0070"
down_revision = "0069"
branch_labels: tuple[str, ...] = ()
depends_on: tuple[str, ...] = ()


def upgrade() -> None:
    op.execute("""
        CREATE TABLE outreach_imports (
            tenant_id text NOT NULL,
            import_id text NOT NULL,
            status text NOT NULL DEFAULT 'preview'
                CHECK (status IN ('preview', 'committed', 'discarded')),
            filename text NULL,
            campaign_name text NOT NULL,
            goal text NULL,
            rows jsonb NOT NULL DEFAULT '[]',
            report jsonb NOT NULL DEFAULT '{}',
            consent_attested boolean NOT NULL DEFAULT false,
            attested_by text NULL,
            created_by text NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            committed_at timestamptz NULL,
            PRIMARY KEY (tenant_id, import_id)
        )
    """)
    op.execute("CREATE INDEX ix_outreach_imports_recent ON outreach_imports "
               "(tenant_id, created_at DESC)")
    op.execute("""
        CREATE TABLE outreach_command_drafts (
            tenant_id text NOT NULL,
            draft_id text NOT NULL,
            raw_text text NOT NULL,
            parsed jsonb NOT NULL,
            contact_ids text[] NOT NULL DEFAULT '{}',
            status text NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'confirmed', 'discarded')),
            result jsonb NULL,
            created_by text NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            decided_at timestamptz NULL,
            PRIMARY KEY (tenant_id, draft_id)
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS outreach_command_drafts")
    op.execute("DROP TABLE IF EXISTS outreach_imports")

"""Migration 0073: per-tenant call-recording retention (Sprint A9 H8, compliance §5)."""
from __future__ import annotations

from alembic import op

revision = "0073"
down_revision = "0072"
branch_labels: tuple[str, ...] = ()
depends_on: tuple[str, ...] = ()


def upgrade() -> None:
    op.execute("ALTER TABLE tenant_call_agent_configs ADD COLUMN recording_retention_days "
               "int NOT NULL DEFAULT 90 CHECK (recording_retention_days BETWEEN 1 AND 3650)")
    op.execute("CREATE INDEX ix_call_sessions_recorded ON call_sessions (tenant_id, created_at) "
               "WHERE recording_url IS NOT NULL")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_call_sessions_recorded")
    op.execute("ALTER TABLE tenant_call_agent_configs DROP COLUMN IF EXISTS "
               "recording_retention_days")

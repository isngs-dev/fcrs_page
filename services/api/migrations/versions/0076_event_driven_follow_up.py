"""Migration 0076: e-mail working hours and campaign end dates (Sprint A12)."""
from __future__ import annotations

from alembic import op

revision = "0076"
down_revision = "0075"
branch_labels: tuple[str, ...] = ()
depends_on: tuple[str, ...] = ()


def upgrade() -> None:
    op.execute("ALTER TABLE agent_settings "
               "ADD COLUMN email_window_start time NOT NULL DEFAULT '08:00', "
               "ADD COLUMN email_window_end time NOT NULL DEFAULT '18:00'")
    op.execute("ALTER TABLE outreach_campaigns ADD COLUMN ends_at timestamptz NULL")
    op.execute("CREATE INDEX ix_outreach_campaigns_ending ON outreach_campaigns (ends_at) "
               "WHERE status = 'active' AND ends_at IS NOT NULL")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_outreach_campaigns_ending")
    op.execute("ALTER TABLE outreach_campaigns DROP COLUMN IF EXISTS ends_at")
    op.execute("ALTER TABLE agent_settings DROP COLUMN IF EXISTS email_window_start, "
               "DROP COLUMN IF EXISTS email_window_end")

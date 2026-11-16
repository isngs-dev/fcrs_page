"""Migration 0078: who on the client's team is emailed when a lead doesn't pick up.

Owner, 2026-11-16: the team holds the business phone; after each call Rebecca couldn't get
through (no answer / voicemail) these addresses get "please call this lead". Empty = off.
"""
from __future__ import annotations

from alembic import op

revision = "0078"
down_revision = "0077"
branch_labels: tuple[str, ...] = ()
depends_on: tuple[str, ...] = ()


def upgrade() -> None:
    op.execute("ALTER TABLE tenant_call_agent_configs "
               "ADD COLUMN missed_call_alert_emails text[] NOT NULL DEFAULT '{}'")


def downgrade() -> None:
    op.execute("ALTER TABLE tenant_call_agent_configs DROP COLUMN missed_call_alert_emails")

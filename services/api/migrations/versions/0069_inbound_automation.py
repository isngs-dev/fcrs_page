"""Migration 0069: inbound automation opt-in (Sprint A4).

``agent_settings.inbound_auto_enabled`` -- off by default; when on, widget
leads that tick a channel consent box get an ``inbound_auto`` outreach plan.
"""
from __future__ import annotations

from alembic import op

revision = "0069"
down_revision = "0068"
branch_labels: tuple[str, ...] = ()
depends_on: tuple[str, ...] = ()


def upgrade() -> None:
    op.execute("ALTER TABLE agent_settings "
               "ADD COLUMN inbound_auto_enabled boolean NOT NULL DEFAULT false")


def downgrade() -> None:
    op.execute("ALTER TABLE agent_settings DROP COLUMN IF EXISTS inbound_auto_enabled")

"""Migration 0038: optional tenant workspace labels (SR-8).

SR-8's original specification called this revision 0037, but SR-7 already
owns 0037 for ``launcher_label``.  These two independent admin-console labels
therefore build on SR-7's revision without changing its widget schema.
"""
from __future__ import annotations

from alembic import op

revision = "0038"
down_revision = "0037"
branch_labels: tuple[str, ...] = ()
depends_on: tuple[str, ...] = ()


def upgrade() -> None:
    op.execute("ALTER TABLE tenant_bot_settings ADD COLUMN sidebar_workspace_label text")
    op.execute("ALTER TABLE tenant_bot_settings ADD COLUMN dashboard_title text")


def downgrade() -> None:
    op.execute("ALTER TABLE tenant_bot_settings DROP COLUMN IF EXISTS dashboard_title")
    op.execute("ALTER TABLE tenant_bot_settings DROP COLUMN IF EXISTS sidebar_workspace_label")

"""Migration 0065: widget text colors -- bot reply text, visitor bubble text.

Same style as 0059: plain nullable ``ALTER TABLE ... ADD COLUMN``; NULL means
"the widget's built-in color".
"""
from __future__ import annotations

from alembic import op

revision = "0065"
down_revision = "0064"
branch_labels: tuple[str, ...] = ()
depends_on: tuple[str, ...] = ()


def upgrade() -> None:
    op.execute("ALTER TABLE tenant_bot_settings ADD COLUMN bot_text_color text")
    op.execute("ALTER TABLE tenant_bot_settings ADD COLUMN visitor_text_color text")


def downgrade() -> None:
    op.execute("ALTER TABLE tenant_bot_settings DROP COLUMN IF EXISTS visitor_text_color")
    op.execute("ALTER TABLE tenant_bot_settings DROP COLUMN IF EXISTS bot_text_color")

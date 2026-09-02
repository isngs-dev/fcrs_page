"""Migration 0059: tenant widget branding -- bot name, accent color,
launcher position, suggested questions.

Same style as 0037/0038: plain nullable ``ALTER TABLE ... ADD COLUMN``, no
default fabrication. ``suggested_questions`` is jsonb (a JSON array of
strings), mirroring the existing ``business_hours jsonb`` precedent.
"""
from __future__ import annotations

from alembic import op

revision = "0059"
down_revision = "0058"
branch_labels: tuple[str, ...] = ()
depends_on: tuple[str, ...] = ()


def upgrade() -> None:
    op.execute("ALTER TABLE tenant_bot_settings ADD COLUMN bot_name text")
    op.execute("ALTER TABLE tenant_bot_settings ADD COLUMN accent_color text")
    op.execute("ALTER TABLE tenant_bot_settings ADD COLUMN launcher_position text")
    op.execute("ALTER TABLE tenant_bot_settings ADD COLUMN suggested_questions jsonb")


def downgrade() -> None:
    op.execute("ALTER TABLE tenant_bot_settings DROP COLUMN IF EXISTS suggested_questions")
    op.execute("ALTER TABLE tenant_bot_settings DROP COLUMN IF EXISTS launcher_position")
    op.execute("ALTER TABLE tenant_bot_settings DROP COLUMN IF EXISTS accent_color")
    op.execute("ALTER TABLE tenant_bot_settings DROP COLUMN IF EXISTS bot_name")

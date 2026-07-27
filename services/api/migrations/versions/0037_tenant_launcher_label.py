"""Migration 0037: optional tenant launcher label (SR-7).

The label is presentation copy for the embeddable widget's launcher pill.
It remains nullable so existing tenant rows retain the widget's explicit
``Chat with us`` presentation default until a CLIENT_ADMIN configures it.
"""
from __future__ import annotations

from alembic import op

revision = "0037"
down_revision = "0036"
branch_labels: tuple[str, ...] = ()
depends_on: tuple[str, ...] = ()


def upgrade() -> None:
    op.execute("ALTER TABLE tenant_bot_settings ADD COLUMN launcher_label text")


def downgrade() -> None:
    op.execute("ALTER TABLE tenant_bot_settings DROP COLUMN IF EXISTS launcher_label")

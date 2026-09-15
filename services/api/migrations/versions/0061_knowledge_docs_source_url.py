"""Migration 0061: knowledge_docs.source_url -- the URL a "url"-sourced
knowledge doc was fetched from (add-a-website feature). Nullable, no
backfill -- every existing row is an "upload" doc and stays NULL.
"""
from __future__ import annotations

from alembic import op

revision = "0061"
down_revision = "0060"
branch_labels: tuple[str, ...] = ()
depends_on: tuple[str, ...] = ()


def upgrade() -> None:
    op.execute("ALTER TABLE knowledge_docs ADD COLUMN source_url text")


def downgrade() -> None:
    op.execute("ALTER TABLE knowledge_docs DROP COLUMN IF EXISTS source_url")

"""Migration 0046: accelerate literal lead Name/Email search (SR-25).

The lead-list ``q`` parameter uses case-insensitive substring predicates, so
ordinary btree indexes cannot serve it. pg_trgm GIN indexes cover nullable
``name`` and ``email`` safely; NULL rows simply have no index entry.
"""
from __future__ import annotations

from alembic import op

revision = "0046"
down_revision = "0045"
branch_labels: tuple[str, ...] = ()
depends_on: tuple[str, ...] = ()


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute("CREATE INDEX idx_leads_name_trgm ON leads USING gin (name gin_trgm_ops)")
    op.execute("CREATE INDEX idx_leads_email_trgm ON leads USING gin (email gin_trgm_ops)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_leads_email_trgm")
    op.execute("DROP INDEX IF EXISTS idx_leads_name_trgm")
    # Do not drop pg_trgm: extensions are shared database capability and a
    # later feature may depend on it after this migration is rolled back.

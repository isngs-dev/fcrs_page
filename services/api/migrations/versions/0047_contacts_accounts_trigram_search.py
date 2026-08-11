"""Migration 0047: accelerate Contacts/Accounts Name/Email/Domain search (SR-29).

Mirrors 0046 (SR-25) for the CRM entity tables. The `q` parameters on
GET /admin/contacts and GET /admin/accounts use case-insensitive substring
predicates, which ordinary btree indexes cannot serve. pg_trgm GIN indexes
cover nullable columns safely -- NULL rows simply have no index entry.
"""
from __future__ import annotations

from alembic import op

revision = "0047"
down_revision = "0046"
branch_labels: tuple[str, ...] = ()
depends_on: tuple[str, ...] = ()


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute("CREATE INDEX idx_contacts_name_trgm ON contacts USING gin (name gin_trgm_ops)")
    op.execute("CREATE INDEX idx_contacts_email_trgm ON contacts USING gin (email gin_trgm_ops)")
    op.execute("CREATE INDEX idx_accounts_name_trgm ON accounts USING gin (name gin_trgm_ops)")
    op.execute("CREATE INDEX idx_accounts_domain_trgm ON accounts USING gin (domain gin_trgm_ops)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_accounts_domain_trgm")
    op.execute("DROP INDEX IF EXISTS idx_accounts_name_trgm")
    op.execute("DROP INDEX IF EXISTS idx_contacts_email_trgm")
    op.execute("DROP INDEX IF EXISTS idx_contacts_name_trgm")
    # Do not drop pg_trgm: extensions are shared database capability and
    # SR-25's lead indexes still depend on it.

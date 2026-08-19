"""Add title, description, uploaded_by to knowledge_docs.

Raw SQL migration (no ORM models / no autogenerate).

Admin-facing Knowledge Base list feature: admins can now optionally name a
knowledge item (title) and describe it (description) at upload time -- both
nullable; the admin-web UI falls back to the raw filename when title is
blank. uploaded_by records the uploading admin's user id (AuthClaims.subject,
including the real platform-admin id on the PLATFORM_ADMIN tenant-scoped
upload path) for provenance/display in the new list endpoint.

uploaded_by intentionally has NO foreign key to users(id): the list endpoint
resolves it to a display name via a best-effort auth.repository.get_user_by_id
lookup and falls back to showing the raw id if the user row is gone, so a FK
(which would either block user deletion or require ON DELETE SET NULL wiring)
isn't needed.

All three columns are NULL for every existing row -- no backfill, no
behavior change for documents uploaded before this migration.
"""
from __future__ import annotations

from alembic import op

revision = "0054"
down_revision = "0053"
branch_labels: tuple[str, ...] = ()
depends_on: tuple[str, ...] = ()


def upgrade() -> None:
    op.execute("ALTER TABLE knowledge_docs ADD COLUMN title text")
    op.execute("ALTER TABLE knowledge_docs ADD COLUMN description text")
    op.execute("ALTER TABLE knowledge_docs ADD COLUMN uploaded_by text")


def downgrade() -> None:
    op.execute("ALTER TABLE knowledge_docs DROP COLUMN uploaded_by")
    op.execute("ALTER TABLE knowledge_docs DROP COLUMN description")
    op.execute("ALTER TABLE knowledge_docs DROP COLUMN title")

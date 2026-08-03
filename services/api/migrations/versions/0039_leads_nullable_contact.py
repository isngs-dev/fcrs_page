"""Migration 0039: leads.name/leads.email become nullable (SR-9.1 C4).

SR-9.1's spec originally called this revision "0037", but that number was
already taken by ``0037_tenant_launcher_label`` (SR-7) by the time this
sprint landed -- the same renumbering situation ``0038`` documented for SR-8.
This migration continues the chain from the current head (0038).

Live-schema check (SR-9.1 DoD, evidence before belief): ``\\d leads`` against
the running dev Postgres confirmed both ``name`` and ``email`` are currently
``NOT NULL`` (as created by migration 0014) -- so this migration is required,
not optional.

Booking-lead autolink (SR-9.1) creates a lead from an anonymous booking (no
email/name supplied) with NULL contact fields rather than a fabricated
placeholder (no-silent-fallback / no-fake-data). Dropping the NOT NULL
constraint is required for that INSERT to succeed. No data change --
existing rows are unaffected (they already have non-NULL name/email).
"""
from __future__ import annotations

from alembic import op

revision = "0039"
down_revision = "0038"
branch_labels: tuple[str, ...] = ()
depends_on: tuple[str, ...] = ()


def upgrade() -> None:
    op.execute("ALTER TABLE leads ALTER COLUMN name DROP NOT NULL")
    op.execute("ALTER TABLE leads ALTER COLUMN email DROP NOT NULL")


def downgrade() -> None:
    # Restoring NOT NULL would fail if any anonymous booking-created lead
    # rows exist with NULL name/email -- this is a forward-only migration in
    # practice; the downgrade is provided for symmetry/dev rollback only.
    op.execute("ALTER TABLE leads ALTER COLUMN name SET NOT NULL")
    op.execute("ALTER TABLE leads ALTER COLUMN email SET NOT NULL")

"""Migration 0048: leads.email_verdict / email_verdict_reason / email_verdict_at.

Adds automated email-qualification columns to ``leads`` (Leads > Board email
qualification feature). Additive/nullable, no CHECK constraint -- matches the
existing convention that ``stage``/``status`` allowed values are enforced in
application code (``api.leads.pipeline``), not the database (see migration
0014's docstring). ``email_verdict`` is one of ``qualified`` / ``disqualified``
/ ``needs_review``, or ``NULL`` for a lead never classified (including every
existing row until the backfill script runs, and any lead whose ``email`` is
NULL -- classification is skipped entirely for those, per SR-9.1's anonymous
booking-lead design).
"""
from __future__ import annotations

from alembic import op

revision = "0048"
down_revision = "0047"
branch_labels: tuple[str, ...] = ()
depends_on: tuple[str, ...] = ()


def upgrade() -> None:
    op.execute("ALTER TABLE leads ADD COLUMN email_verdict text")
    op.execute("ALTER TABLE leads ADD COLUMN email_verdict_reason text")
    op.execute("ALTER TABLE leads ADD COLUMN email_verdict_at timestamptz")


def downgrade() -> None:
    op.execute("ALTER TABLE leads DROP COLUMN email_verdict_at")
    op.execute("ALTER TABLE leads DROP COLUMN email_verdict_reason")
    op.execute("ALTER TABLE leads DROP COLUMN email_verdict")

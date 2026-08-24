"""Migration 0057: allow training_answers rows to record a dismissal.

Raw SQL migration (no ORM models / no autogenerate) -- same style as 0056.

Adds a "dismiss this coverage gap without teaching an answer" path: an admin
reviewing the coverage-gaps feed may decide a question is junk/adversarial
(e.g. a visitor typing "I won't.") rather than a real gap worth an answer.
Dismissing it must still remove it from the feed -- the existing exclusion
query already filters on ANY row in ``training_answers`` matching the
normalized question, so a dismissal reuses that same table and mechanism
rather than inventing a second one.

``answer`` becomes nullable (a dismissal has no answer); ``dismissed
boolean NOT NULL DEFAULT false`` distinguishes the two row kinds explicitly
rather than inferring it from ``answer IS NULL`` (an implicit convention is
easy to violate by accident later). A CHECK constraint enforces the
invariant defense-in-depth: every row is either dismissed, or has a real
answer -- never neither, which would silently break the feed's
"presence in this table means handled" assumption.
"""
from __future__ import annotations

from alembic import op

revision = "0057"
down_revision = "0056"
branch_labels: tuple[str, ...] = ()
depends_on: tuple[str, ...] = ()


def upgrade() -> None:
    op.execute("ALTER TABLE training_answers ALTER COLUMN answer DROP NOT NULL")
    op.execute("ALTER TABLE training_answers ADD COLUMN dismissed boolean NOT NULL DEFAULT false")
    op.execute(
        "ALTER TABLE training_answers "
        "ADD CONSTRAINT ck_training_answers_dismissed_or_answered "
        "CHECK (dismissed = true OR answer IS NOT NULL)"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE training_answers "
        "DROP CONSTRAINT IF EXISTS ck_training_answers_dismissed_or_answered"
    )
    op.execute("ALTER TABLE training_answers DROP COLUMN dismissed")
    op.execute("ALTER TABLE training_answers ALTER COLUMN answer SET NOT NULL")

"""Migration 0077: a call booked on a website form is a schedule event (source ``form``).

The client's site books it (and sends its own Meet invite); intake records it so the slot
counts as taken and the calling agent offers to keep or reschedule it instead of booking.
``calendar_ref`` is ``form:<intake_id>``; re-delivery is stopped by the intake event's own
(source, external id) uniqueness.
"""
from __future__ import annotations

from alembic import op

revision = "0077"
down_revision = "0076"
branch_labels: tuple[str, ...] = ()
depends_on: tuple[str, ...] = ()


def upgrade() -> None:
    op.execute("ALTER TABLE schedule_events DROP CONSTRAINT schedule_events_source_check")
    op.execute("ALTER TABLE schedule_events ADD CONSTRAINT schedule_events_source_check "
               "CHECK (source IN ('native', 'calendly', 'form'))")


def downgrade() -> None:
    op.execute("DELETE FROM schedule_events WHERE source = 'form'")
    op.execute("ALTER TABLE schedule_events DROP CONSTRAINT schedule_events_source_check")
    op.execute("ALTER TABLE schedule_events ADD CONSTRAINT schedule_events_source_check "
               "CHECK (source IN ('native', 'calendly'))")

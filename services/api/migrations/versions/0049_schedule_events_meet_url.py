"""Migration 0049: schedule_events.meet_url.

Adds the Google Meet join URL to a booked event (SR-22). Additive/nullable
-- populated only after a successful ``CalendarProvider.create_event`` call
that returned one (``api.scheduling.routes.book_slot``,
``update_event_calendar_ref``); every existing row and every event whose
calendar provider doesn't create a conference (or has no calendar configured
at all) stays NULL, never a fabricated link. Read by
``api.scheduling.repository.get_event_contact`` so both the booking
confirmation and the 3d/24h/1h reminder emails can include it.
"""
from __future__ import annotations

from alembic import op

revision = "0049"
down_revision = "0048"
branch_labels: tuple[str, ...] = ()
depends_on: tuple[str, ...] = ()


def upgrade() -> None:
    op.execute("ALTER TABLE schedule_events ADD COLUMN meet_url text")


def downgrade() -> None:
    op.execute("ALTER TABLE schedule_events DROP COLUMN meet_url")

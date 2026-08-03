"""Migration 0041: notification_jobs.lead_id (SR-9.3).

Raw SQL migration (no ORM models / no autogenerate), matching the house
style of 0040/0014.

Revision number determined by reading the live migration head
(``alembic heads``) immediately before creating this file. The head was
0040 (``0040_contacts_accounts_entities.py`` -- SR-9.2, already shipped), so
this migration chains from 0040.

SR-9.3 (unified customer-360 timeline) needs an honest, forward-only join
from a notification job back to the lead it was sent about. Per the sprint's
D4/J3/J4:

- ``notification_jobs`` has NO existing attribution handle (no ``lead_id``,
  ``contact_id``, ``visitor_id``, or ``event_id`` column -- only
  ``payload->>'event_id'`` for booking/reminder kinds).
- ``notification_jobs`` breaks the composite-PK convention every other
  table follows: it has a singular ``job_id text PRIMARY KEY`` with
  ``tenant_id`` as an ordinary column, so a composite FK to
  ``leads(tenant_id, lead_id)`` is NOT representable here. This column is a
  plain nullable link enforced by query predicate (always
  ``WHERE tenant_id = $1 AND lead_id = ANY($2)``), a weaker guarantee than
  the rest of the system -- tested explicitly, not assumed safe.

No backfill of historical rows (D4) -- populated forward-only at exactly two
write sites (``notifications/reminder_sink.py``, ``scheduling/routes.py``).
"""
from __future__ import annotations

from alembic import op

revision = "0041"
down_revision = "0040"
branch_labels: tuple[str, ...] = ()
depends_on: tuple[str, ...] = ()


def upgrade() -> None:
    op.execute("ALTER TABLE notification_jobs ADD COLUMN lead_id text")
    op.execute(
        "CREATE INDEX idx_notification_jobs_tenant_lead "
        "ON notification_jobs (tenant_id, lead_id) WHERE lead_id IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_notification_jobs_tenant_lead")
    op.execute("ALTER TABLE notification_jobs DROP COLUMN IF EXISTS lead_id")

"""Migration 0045: add tenants.timezone + tenant_assignment_configs (SR-20).

D8: this revision number was determined by reading the live migration head
(``alembic heads``) immediately before creating this file. The head was
0044 (``0044_notification_events.py``, landed by the concurrent SR-21
attempt) -- verified live rather than assumed or taken from the spec, per
the repeated house rule (SR-9.4 D11 / SR-20 D8 itself).

Two independent additions:

1. ``tenants.timezone`` (D5, AS AMENDED by the user's resolved open
   question): a NEW nullable column, an IANA timezone identifier
   (``Europe/London``), app-validated in ``api.admin.workspace_repository``
   before ever reaching the DB -- never a raw offset. NULLABLE, no backfill:
   an unconfigured tenant resolves to a platform default from Pydantic
   Settings (``workspace_default_timezone``) at read time, never a guessed
   value written here (CLAUDE.md §3 -- no silent fabrication).

   NOTE ON SCOPE DEVIATION FROM THE ORIGINAL SPEC TEXT: the spec's Data
   Model section (as originally written) also called for a
   ``tenants.language`` column. The user's own "Open questions" section
   flagged that a stored ``language`` preference with no i18n consumer is
   uncomfortably close to the silent-no-op D1 exists to forbid, and asked
   for a decision. That question has now been resolved: Language renders
   disabled/coming-soon in the Settings UI, exactly like Billing and
   Delete-workspace (D7) -- NOT a live stored field. Since the field is
   inert, there is nothing to store, so this migration deliberately does
   NOT add a ``language`` column. This is a live deviation from the
   spec's original Data Model text, stated here explicitly per the
   sprint's instructions.

2. ``tenant_assignment_configs`` (D1/D4): one row per tenant holding the
   round-robin enable flag AND the persisted rotation cursor in the SAME
   table/row, so ``api.leads.assignment.select_next_agent``'s single atomic
   ``UPDATE ... RETURNING`` can advance the cursor without a read-then-write
   across two tables (D4's concurrency requirement).

Raw SQL migration (no ORM models / no autogenerate) -- same style as
0027/0040/0041/0043/0044.
"""
from __future__ import annotations

from alembic import op

revision = "0045"
down_revision = "0044"
branch_labels: tuple[str, ...] = ()
depends_on: tuple[str, ...] = ()


def upgrade() -> None:
    op.execute("ALTER TABLE tenants ADD COLUMN timezone text")

    op.execute(
        """
        CREATE TABLE tenant_assignment_configs (
          tenant_id               text PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
          round_robin_enabled     boolean     NOT NULL DEFAULT false,
          last_assigned_agent_id  text,
          created_at              timestamptz NOT NULL DEFAULT now(),
          updated_at              timestamptz NOT NULL DEFAULT now()
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS tenant_assignment_configs")
    op.execute("ALTER TABLE tenants DROP COLUMN IF EXISTS timezone")

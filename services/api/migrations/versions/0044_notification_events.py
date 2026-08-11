"""Migration 0044: add notification_events + notification_event_reads (SR-21).

Raw SQL migration (no ORM models / no autogenerate) -- same style as
0027/0040/0041/0043.

D9: this revision number was determined by reading the live migration head
(``alembic heads``) immediately before creating this file. The head was
0043 (``0043_orchestrator_identity_gate.py``), matching the spec's M10 note
at time of writing -- verified live rather than assumed, per the repeated
house rule (SR-9.4 D11 / SR-20 D8).

SR-21 (the in-console notifications feed). Two NEW tables, deliberately
distinct from the EXISTING ``notification_jobs`` outbound delivery ledger
(0021_notifications.py) -- D1. Nothing here has a recipient, subject, body,
channel or delivery status; ``notification_jobs`` is untouched by this
migration.

- ``notification_events``: the tenant-scoped, in-console feed of "things
  that happened in this workspace" (D3's closed, app-validated ``kind``
  vocabulary -- no DB CHECK, mirroring the ``lead_activities.type``
  precedent so the vocabulary can grow without a migration). ``payload`` is
  PII-free by construction (ids only) -- enforced at the repository/emit
  layer, not the schema.
- ``notification_event_reads``: PER-USER read state (D6) -- read/unread is
  NEVER shared across users in the same tenant. Composite PK
  ``(tenant_id, event_id, user_id)`` plus a composite FK back to
  ``notification_events(tenant_id, event_id)`` makes a cross-tenant
  read-state row unrepresentable (not merely disallowed), mirroring the
  SR-9.4 M9 guarantee. ``ON DELETE CASCADE`` is deliberate here (unlike the
  SR-9.2 K5 lesson against cascades in general): a read-state row has no
  meaning without its event, so pruning old events (D7) must take their
  read rows with them.
"""
from __future__ import annotations

from alembic import op

revision = "0044"
down_revision = "0043"
branch_labels: tuple[str, ...] = ()
depends_on: tuple[str, ...] = ()


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE notification_events (
          tenant_id   text        NOT NULL,
          event_id    text        NOT NULL,
          kind        text        NOT NULL,
          category    text        NOT NULL,
          target_type text,
          target_id   text,
          payload     jsonb,
          actor_id    text,
          created_at  timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (tenant_id, event_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_notification_events_tenant_created "
        "ON notification_events (tenant_id, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX idx_notification_events_tenant_category_created "
        "ON notification_events (tenant_id, category, created_at DESC)"
    )

    op.execute(
        """
        CREATE TABLE notification_event_reads (
          tenant_id text        NOT NULL,
          event_id  text        NOT NULL,
          user_id   text        NOT NULL,
          read_at   timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (tenant_id, event_id, user_id),
          FOREIGN KEY (tenant_id, event_id)
            REFERENCES notification_events (tenant_id, event_id) ON DELETE CASCADE
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS notification_event_reads")
    op.execute("DROP TABLE IF EXISTS notification_events")

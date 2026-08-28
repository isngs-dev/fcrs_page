"""Migration 0058: tenant_call_configs (missed-call text-back).

Raw SQL migration (no ORM models / no autogenerate) -- same style as 0057.

One row per tenant: which of their Twilio numbers to watch for missed calls
(``monitored_phone_number``), whether the feature is on (``enabled``,
defaults false -- explicit opt-in even if the tenant already has Twilio SMS
configured for other notification purposes), and the admin's own free-text
text-back message (``text_back_message`` -- may contain their own link;
never auto-generated, no secret/credential content, so unlike
``tenant_calendar_configs``/``tenant_notification_configs`` nothing here is
encrypted at rest).
"""
from __future__ import annotations

from alembic import op

revision = "0058"
down_revision = "0057"
branch_labels: tuple[str, ...] = ()
depends_on: tuple[str, ...] = ()


def upgrade() -> None:
    op.execute(
        "CREATE TABLE tenant_call_configs ("
        "tenant_id text PRIMARY KEY, "
        "monitored_phone_number text NOT NULL, "
        "enabled boolean NOT NULL DEFAULT false, "
        "text_back_message text NOT NULL, "
        "created_at timestamptz NOT NULL DEFAULT now(), "
        "updated_at timestamptz NOT NULL DEFAULT now()"
        ")"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS tenant_call_configs")

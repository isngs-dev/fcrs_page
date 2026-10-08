"""Migration 0062: AI voice confirmation calls.

Raw SQL migration (no ORM models / no autogenerate) -- same style as 0058.

``tenant_voice_call_configs``: one row per tenant -- whether a booking
triggers an automated confirmation call (``enabled``, default false: explicit
opt-in) and the admin-editable yes/no ``questions`` (a JSON array of strings).

``voice_calls``: one row per booking call. ``questions`` is the rendered
snapshot asked on this call (an admin editing the config mid-call can't
change it). ``transcript`` is a JSON object keyed by question index so a
Twilio webhook retry overwrites the same entry instead of duplicating it.
UNIQUE (tenant_id, event_id): a booking is called at most once.
"""
from __future__ import annotations

from alembic import op

revision = "0062"
down_revision = "0061"
branch_labels: tuple[str, ...] = ()
depends_on: tuple[str, ...] = ()


def upgrade() -> None:
    op.execute(
        "CREATE TABLE tenant_voice_call_configs ("
        "tenant_id text PRIMARY KEY, "
        "enabled boolean NOT NULL DEFAULT false, "
        "questions jsonb NOT NULL, "
        "created_at timestamptz NOT NULL DEFAULT now(), "
        "updated_at timestamptz NOT NULL DEFAULT now()"
        ")"
    )
    op.execute(
        "CREATE TABLE voice_calls ("
        "call_id text PRIMARY KEY, "
        "tenant_id text NOT NULL, "
        "event_id text NOT NULL, "
        "lead_id text, "
        "to_number text NOT NULL, "
        "questions jsonb NOT NULL, "
        "status text NOT NULL DEFAULT 'queued' "
        "CHECK (status IN ('queued', 'calling', 'in_progress', 'completed', "
        "'no_answer', 'busy', 'failed')), "
        "twilio_call_sid text, "
        "transcript jsonb NOT NULL DEFAULT '{}'::jsonb, "
        "last_error text, "
        "created_at timestamptz NOT NULL DEFAULT now(), "
        "updated_at timestamptz NOT NULL DEFAULT now(), "
        "UNIQUE (tenant_id, event_id)"
        ")"
    )
    op.execute(
        "CREATE INDEX idx_voice_calls_tenant_lead ON voice_calls (tenant_id, lead_id) "
        "WHERE lead_id IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS voice_calls")
    op.execute("DROP TABLE IF EXISTS tenant_voice_call_configs")

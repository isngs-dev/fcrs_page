"""Migration 0063: AI voice agent ("Call Us" in the widget).

Raw SQL migration (no ORM models / no autogenerate) -- same style as 0062.

``tenant_voice_agent_configs``: one row per tenant -- whether the widget shows
"Call Us" (``enabled``, default false: explicit opt-in), the spoken greeting
(NULL -> the built-in default), the number a call is transferred to, the
team's business hours (transfers only ring inside them; outside, the caller
is offered "Schedule a Call") and the call-length limit.

``voice_agent_calls``: one row per browser call. The transcript itself lives
in the visitor's conversation (``messages`` rows tagged intent
'voice_call'), so admins read it in Conversations next to the chat; this row
carries the call-level facts: outcome, transfer reason, timing.
"""
from __future__ import annotations

from alembic import op

revision = "0063"
down_revision = "0062"
branch_labels: tuple[str, ...] = ()
depends_on: tuple[str, ...] = ()


def upgrade() -> None:
    op.execute(
        "CREATE TABLE tenant_voice_agent_configs ("
        "tenant_id text PRIMARY KEY, "
        "enabled boolean NOT NULL DEFAULT false, "
        "greeting text, "
        "transfer_number text NOT NULL DEFAULT '+17868233553', "
        "timezone text NOT NULL DEFAULT 'America/New_York', "
        "open_time text NOT NULL DEFAULT '09:00', "
        "close_time text NOT NULL DEFAULT '18:00', "
        "open_days int[] NOT NULL DEFAULT '{0,1,2,3,4}', "
        "max_minutes int NOT NULL DEFAULT 10 CHECK (max_minutes BETWEEN 1 AND 60), "
        "created_at timestamptz NOT NULL DEFAULT now(), "
        "updated_at timestamptz NOT NULL DEFAULT now()"
        ")"
    )
    op.execute(
        "CREATE TABLE voice_agent_calls ("
        "call_id text PRIMARY KEY, "
        "tenant_id text NOT NULL, "
        "visitor_id text NOT NULL, "
        "conversation_id text NOT NULL, "
        "status text NOT NULL DEFAULT 'starting' "
        "CHECK (status IN ('starting', 'in_progress', 'completed', 'transferring', "
        "'transferred', 'transfer_unanswered', 'after_hours', 'failed')), "
        "transfer_reason text, "
        "twilio_call_sid text, "
        "started_at timestamptz, "
        "ended_at timestamptz, "
        "created_at timestamptz NOT NULL DEFAULT now(), "
        "updated_at timestamptz NOT NULL DEFAULT now()"
        ")"
    )
    op.execute(
        "CREATE INDEX idx_voice_agent_calls_tenant_conv "
        "ON voice_agent_calls (tenant_id, conversation_id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS voice_agent_calls")
    op.execute("DROP TABLE IF EXISTS tenant_voice_agent_configs")

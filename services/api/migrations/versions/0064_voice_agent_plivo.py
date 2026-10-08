"""Migration 0064: AI voice agent moves from Twilio to Plivo.

Raw SQL migration -- same style as 0063.

- ``twilio_call_sid`` -> ``plivo_call_uuid`` (Plivo's ``CallUUID``), indexed:
  the hangup webhook finds its call by it.
- ``session jsonb``: the agent's per-call state (misses, last question,
  recent turns). Twilio held it in a live WebSocket; Plivo posts each caller
  turn as a separate webhook, so it lives on the row between turns.
"""
from __future__ import annotations

from alembic import op

revision = "0064"
down_revision = "0063"
branch_labels: tuple[str, ...] = ()
depends_on: tuple[str, ...] = ()


def upgrade() -> None:
    op.execute("ALTER TABLE voice_agent_calls RENAME COLUMN twilio_call_sid TO plivo_call_uuid")
    op.execute("ALTER TABLE voice_agent_calls ADD COLUMN session jsonb")
    op.execute(
        "CREATE INDEX idx_voice_agent_calls_plivo_call_uuid ON voice_agent_calls (plivo_call_uuid)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_voice_agent_calls_plivo_call_uuid")
    op.execute("ALTER TABLE voice_agent_calls DROP COLUMN session")
    op.execute("ALTER TABLE voice_agent_calls RENAME COLUMN plivo_call_uuid TO twilio_call_sid")

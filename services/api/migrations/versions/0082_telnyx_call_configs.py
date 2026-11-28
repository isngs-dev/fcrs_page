"""Migration 0082: a chatbot on Telnyx has no Plivo token (owner, 2026-11-28).

Every chatbot calls through Telnyx (platform keys), so its call settings are saved without a
Plivo Auth ID or token. The token column may now be empty; the API still requires it on the
first save of a chatbot that calls through Plivo.
"""
from __future__ import annotations

from alembic import op

revision = "0082"
down_revision = "0081"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE tenant_call_agent_configs "
               "ALTER COLUMN plivo_auth_token_ciphertext DROP NOT NULL")


def downgrade() -> None:
    # Fails while a Telnyx chatbot has no token: give it one (or delete its row) first.
    op.execute("ALTER TABLE tenant_call_agent_configs "
               "ALTER COLUMN plivo_auth_token_ciphertext SET NOT NULL")

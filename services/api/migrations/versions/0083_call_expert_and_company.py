"""Migration 0083: each calling chatbot names its own expert and says whether to ask for a
company (live 2026-11-28).

A homeowner on the HomeOwners chatbot was asked "which company are you with?" and told "our
marketing expert" would answer -- wording written for chatbots whose leads are businesses.
The defaults keep every existing chatbot exactly as it was.
"""
from __future__ import annotations

from alembic import op

revision = "0083"
down_revision = "0082"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE tenant_call_agent_configs "
               "ADD COLUMN expert_title text NOT NULL DEFAULT 'marketing expert', "
               "ADD COLUMN ask_company boolean NOT NULL DEFAULT true")


def downgrade() -> None:
    op.execute("ALTER TABLE tenant_call_agent_configs "
               "DROP COLUMN expert_title, DROP COLUMN ask_company")

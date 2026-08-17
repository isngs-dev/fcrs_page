"""Add embedding_base_url column to tenant_llm_configs.

Raw SQL migration (no ORM models / no autogenerate).

Lets a tenant's embedding calls target a different OpenAI-wire endpoint than
its chat/classify/stream calls (SR-24: self-hosted companion embedding
container for models with no hosted API, e.g. sentence-transformers). NULL
(the default for every existing row) preserves current behavior exactly --
``OpenAICompatibleProvider`` falls back to ``base_url`` when unset.
"""
from __future__ import annotations

from alembic import op

revision = "0050"
down_revision = "0049"
branch_labels: tuple[str, ...] = ()
depends_on: tuple[str, ...] = ()


def upgrade() -> None:
    op.execute("ALTER TABLE tenant_llm_configs ADD COLUMN embedding_base_url text")


def downgrade() -> None:
    op.execute("ALTER TABLE tenant_llm_configs DROP COLUMN embedding_base_url")

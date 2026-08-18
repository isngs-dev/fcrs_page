"""Add embedding_api_key_ciphertext + embedding_dimensions to tenant_llm_configs.

Raw SQL migration (no ORM models / no autogenerate).

SR-26: a companion embedding service (e.g. the self-hosted all-MiniLM
container) needs no real auth, so embedding_base_url alone (migration 0050)
was enough to route embed calls elsewhere. Pointing embeddings at a REAL
hosted provider with its own credential (OpenAI's actual API, distinct from
whatever provider/key the tenant uses for chat -- e.g. Ollama Cloud) means
the embed call needs its OWN API key, not the chat client's. Encrypted at
rest (AES-256-GCM via SecretBox), mirroring api_key_ciphertext exactly --
NULL (every existing row) falls back to the tenant's main api_key,
preserving current behavior unchanged.

embedding_dimensions (nullable int, NOT encrypted -- not a secret) lets a
tenant request OpenAI's `dimensions` parameter on text-embedding-3-small/
large (Matryoshka truncation) so the output matches whatever the
knowledge_chunks.embedding column's fixed dimension is, without a schema
migration every time the embedding model changes. NULL means "use the
model's native output dimension unchanged" (today's behavior for every
existing row).
"""
from __future__ import annotations

from alembic import op

revision = "0052"
down_revision = "0051"
branch_labels: tuple[str, ...] = ()
depends_on: tuple[str, ...] = ()


def upgrade() -> None:
    op.execute("ALTER TABLE tenant_llm_configs ADD COLUMN embedding_api_key_ciphertext text")
    op.execute("ALTER TABLE tenant_llm_configs ADD COLUMN embedding_dimensions integer")


def downgrade() -> None:
    op.execute("ALTER TABLE tenant_llm_configs DROP COLUMN embedding_dimensions")
    op.execute("ALTER TABLE tenant_llm_configs DROP COLUMN embedding_api_key_ciphertext")

"""Migrate knowledge_chunks.embedding from vector(384) back to vector(768).

Raw SQL migration (no ORM models / no autogenerate).

SR-26: the self-hosted all-MiniLM-L6-v2 companion (migration 0051, 384-dim)
was a stopgap until a real OpenAI key was available. Now switching to
OpenAI's text-embedding-3-small with an explicit `dimensions=768` request
(Matryoshka truncation from its native 1536) -- 768 chosen to match this
column's original pre-0051 dimension, so no downstream code changes.

Existing 384-dim rows cannot be reinterpreted at 768-dim (the values would
be mathematically meaningless under the new model), so this TRUNCATEs
knowledge_chunks before the ALTER -- any previously ingested documents must
be re-ingested to regenerate embeddings under the new model.
``settings.embedding_dimension`` (api/config.py) must be changed to 768
alongside this migration.
"""
from __future__ import annotations

from alembic import op

revision = "0053"
down_revision = "0052"
branch_labels: tuple[str, ...] = ()
depends_on: tuple[str, ...] = ()


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS knowledge_chunks_embedding_hnsw")
    op.execute("TRUNCATE TABLE knowledge_chunks")
    op.execute("ALTER TABLE knowledge_chunks ALTER COLUMN embedding TYPE vector(768)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS knowledge_chunks_embedding_hnsw "
        "ON knowledge_chunks USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS knowledge_chunks_embedding_hnsw")
    op.execute("TRUNCATE TABLE knowledge_chunks")
    op.execute("ALTER TABLE knowledge_chunks ALTER COLUMN embedding TYPE vector(384)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS knowledge_chunks_embedding_hnsw "
        "ON knowledge_chunks USING hnsw (embedding vector_cosine_ops)"
    )

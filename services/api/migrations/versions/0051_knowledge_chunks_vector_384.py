"""Migrate knowledge_chunks.embedding from vector(768) to vector(384).

Raw SQL migration (no ORM models / no autogenerate).

SR-24: the companion embedding container (sentence-transformers/
all-MiniLM-L6-v2) natively outputs 384-dim vectors -- a fixed property of the
model, not a config knob. A pgvector column's dimension is part of its type,
so switching models means changing the type, not just re-populating rows.

Existing 768-dim rows cannot be reinterpreted as 384-dim (the values would be
mathematically meaningless under the new model), so this TRUNCATEs
knowledge_chunks before the ALTER -- any previously ingested documents must
be re-ingested to regenerate embeddings under the new model. This is safe
right now: RAG embeddings never successfully wrote a row on this platform
before today (Ollama Cloud has no /v1/embeddings endpoint at all -- see
orchestrator SR-23), so every environment's table is already empty.

``settings.embedding_dimension`` (api/config.py) must be changed to 384
alongside this migration -- see its own docstring note: "Changing this
requires a new migration + full re-embed."
"""
from __future__ import annotations

from alembic import op

revision = "0051"
down_revision = "0050"
branch_labels: tuple[str, ...] = ()
depends_on: tuple[str, ...] = ()


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS knowledge_chunks_embedding_hnsw")
    op.execute("TRUNCATE TABLE knowledge_chunks")
    op.execute("ALTER TABLE knowledge_chunks ALTER COLUMN embedding TYPE vector(384)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS knowledge_chunks_embedding_hnsw "
        "ON knowledge_chunks USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS knowledge_chunks_embedding_hnsw")
    op.execute("TRUNCATE TABLE knowledge_chunks")
    op.execute("ALTER TABLE knowledge_chunks ALTER COLUMN embedding TYPE vector(768)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS knowledge_chunks_embedding_hnsw "
        "ON knowledge_chunks USING hnsw (embedding vector_cosine_ops)"
    )

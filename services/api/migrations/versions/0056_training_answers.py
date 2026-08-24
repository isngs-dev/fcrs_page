"""Migration 0056: add training_answers (Train the Agent feature).

Raw SQL migration (no ORM models / no autogenerate) -- same style as
0027/0040/0041/0043/0044.

Tracks admin-taught Q&A answers: each row records that a given question has
been addressed, and (once ingestion finishes) which ``knowledge_docs`` row
holds the resulting retrievable content. ``question_normalized`` (lowercase,
whitespace-collapsed) backs an exact-match dedup so the coverage-gaps list
never re-surfaces something already taught. ``doc_id`` is nullable because
the row is written before the async ingestion run necessarily finishes (it
is set at insert time from the newly created doc, so in practice it is
always populated -- nullable only for schema robustness, not because the
write path ever omits it). Composite FK to ``knowledge_docs (tenant_id,
doc_id)`` mirrors that table's composite PK (0010) -- there is no bare
unique constraint on ``doc_id`` alone to reference.
"""
from __future__ import annotations

from alembic import op

revision = "0056"
down_revision = "0055"
branch_labels: tuple[str, ...] = ()
depends_on: tuple[str, ...] = ()


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE training_answers (
          tenant_id           text        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
          id                  text        NOT NULL,
          question            text        NOT NULL,
          question_normalized text        NOT NULL,
          answer              text        NOT NULL,
          source_message_id   text,
          doc_id              text,
          created_by          text        NOT NULL,
          created_at          timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (tenant_id, id),
          FOREIGN KEY (tenant_id, doc_id) REFERENCES knowledge_docs (tenant_id, doc_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_training_answers_tenant_normalized "
        "ON training_answers (tenant_id, question_normalized)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_training_answers_tenant_normalized")
    op.execute("DROP TABLE IF EXISTS training_answers")

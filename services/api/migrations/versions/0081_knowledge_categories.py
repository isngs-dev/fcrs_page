"""Migration 0081: knowledge categories and approval (plan 2.6, owner 2026-11-18).

Each document gets a ``category`` (company, services, ..., pricing, faq, general) and an
``approved`` switch (an upload counts as approval; any admin can switch it off), with who
reviewed it and when. Existing documents become ``general`` + approved: nothing changes for
current clients. ``approved_knowledge_chunks`` is what retrieval reads -- the chunks of
approved documents, each with its document's category -- so the chatbot and the calls get the
same filter without touching the shared vector-search helper.
"""
from __future__ import annotations

from alembic import op

revision = "0081"
down_revision = "0080"
branch_labels: tuple[str, ...] = ()
depends_on: tuple[str, ...] = ()

CATEGORIES = ("company", "services", "industry", "customer_profile", "sales_playbook",
              "case_studies", "pricing", "competitive", "faq", "general")


def upgrade() -> None:
    allowed = ", ".join(f"'{c}'" for c in CATEGORIES)
    op.execute("ALTER TABLE knowledge_docs ADD COLUMN category text NOT NULL DEFAULT 'general' "
               f"CHECK (category IN ({allowed}))")
    op.execute("ALTER TABLE knowledge_docs ADD COLUMN approved boolean NOT NULL DEFAULT true")
    op.execute("ALTER TABLE knowledge_docs ADD COLUMN reviewed_at timestamptz NULL")
    op.execute("ALTER TABLE knowledge_docs ADD COLUMN reviewed_by text NULL")
    op.execute("""
        CREATE VIEW approved_knowledge_chunks AS
        SELECT c.tenant_id, c.doc_id, c.chunk_id, c.content, c.embedding, c.metadata,
               c.created_at, d.category
        FROM knowledge_chunks c
        JOIN knowledge_docs d ON d.tenant_id = c.tenant_id AND d.doc_id = c.doc_id
        WHERE d.approved
    """)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS approved_knowledge_chunks")
    for column in ("reviewed_by", "reviewed_at", "approved", "category"):
        op.execute(f"ALTER TABLE knowledge_docs DROP COLUMN IF EXISTS {column}")

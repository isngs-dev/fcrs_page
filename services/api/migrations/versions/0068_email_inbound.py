"""Migration 0068: Email Agent inbound (Sprint A3).

Reply domain + SendGrid event-webhook verification key on the sender config;
unmatched threads (no contact); classification/auth/action columns on messages.
"""
from __future__ import annotations

from alembic import op

revision = "0068"
down_revision = "0067"
branch_labels: tuple[str, ...] = ()
depends_on: tuple[str, ...] = ()


def upgrade() -> None:
    op.execute("ALTER TABLE tenant_email_agent_configs ADD COLUMN reply_domain text NULL, "
               "ADD COLUMN event_webhook_public_key text NULL")
    op.execute("ALTER TABLE email_threads ALTER COLUMN contact_id DROP NOT NULL")
    op.execute("ALTER TABLE email_threads DROP CONSTRAINT email_threads_status_check")
    op.execute("ALTER TABLE email_threads ADD CONSTRAINT email_threads_status_check "
               "CHECK (status IN ('open', 'closed', 'unmatched'))")
    op.execute("ALTER TABLE email_messages ADD COLUMN classification text NULL, "
               "ADD COLUMN classification_source text NULL, ADD COLUMN reply_text text NULL, "
               "ADD COLUMN auth jsonb NULL, ADD COLUMN handled_action text NULL")
    op.execute("CREATE INDEX ix_email_threads_contact ON email_threads "
               "(tenant_id, contact_id, created_at DESC)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_email_threads_contact")
    op.execute("ALTER TABLE email_messages DROP COLUMN IF EXISTS handled_action, "
               "DROP COLUMN IF EXISTS auth, DROP COLUMN IF EXISTS reply_text, "
               "DROP COLUMN IF EXISTS classification_source, DROP COLUMN IF EXISTS classification")
    op.execute("DELETE FROM email_threads WHERE contact_id IS NULL")
    op.execute("ALTER TABLE email_threads DROP CONSTRAINT email_threads_status_check")
    op.execute("ALTER TABLE email_threads ADD CONSTRAINT email_threads_status_check "
               "CHECK (status IN ('open', 'closed'))")
    op.execute("ALTER TABLE email_threads ALTER COLUMN contact_id SET NOT NULL")
    op.execute("ALTER TABLE tenant_email_agent_configs DROP COLUMN IF EXISTS "
               "event_webhook_public_key, DROP COLUMN IF EXISTS reply_domain")

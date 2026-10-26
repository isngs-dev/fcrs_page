"""Migration 0067: Email Agent outbound (Sprint A2).

Per-tenant sender config (no platform default; SendGrid key AES-GCM encrypted),
email threads (one per outreach plan) and email messages (one per task, with
RFC threading ids and a draft -> approved -> sending -> sent lifecycle).
"""
from __future__ import annotations

from alembic import op

revision = "0067"
down_revision = "0066"
branch_labels: tuple[str, ...] = ()
depends_on: tuple[str, ...] = ()


def upgrade() -> None:
    op.execute("""
        CREATE TABLE tenant_email_agent_configs (
            tenant_id text PRIMARY KEY,
            provider text NOT NULL CHECK (provider IN ('sendgrid', 'smtp')),
            from_address text NOT NULL,
            from_name text NULL,
            reply_to text NULL,
            booking_url text NULL,
            api_key_ciphertext text NULL,
            enabled boolean NOT NULL DEFAULT true,
            updated_at timestamptz NOT NULL DEFAULT now(),
            updated_by text NULL
        )
    """)
    op.execute("""
        CREATE TABLE email_threads (
            tenant_id text NOT NULL,
            thread_id text NOT NULL,
            contact_id text NOT NULL,
            plan_id text NULL,
            subject text NOT NULL,
            status text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
            last_message_at timestamptz NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, thread_id),
            FOREIGN KEY (tenant_id, contact_id)
                REFERENCES contacts (tenant_id, contact_id) ON DELETE CASCADE
        )
    """)
    op.execute("CREATE UNIQUE INDEX uq_email_threads_plan ON email_threads (tenant_id, plan_id) "
               "WHERE plan_id IS NOT NULL")
    op.execute("""
        CREATE TABLE email_messages (
            tenant_id text NOT NULL,
            message_id text NOT NULL,
            thread_id text NOT NULL,
            direction text NOT NULL CHECK (direction IN ('outbound', 'inbound')),
            rfc_message_id text NOT NULL,
            in_reply_to text NULL,
            "references" text[] NULL,
            provider text NULL,
            provider_message_id text NULL,
            from_addr text NOT NULL,
            to_addr text NOT NULL,
            subject text NOT NULL,
            body_text text NOT NULL,
            body_html text NULL,
            status text NOT NULL CHECK (status IN (
                'draft', 'approved', 'sending', 'sent', 'failed', 'discarded', 'received')),
            band text NULL,
            sources jsonb NOT NULL DEFAULT '[]',
            task_id text NULL,
            last_error text NULL,
            approved_by text NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            sent_at timestamptz NULL,
            PRIMARY KEY (tenant_id, message_id),
            UNIQUE (tenant_id, rfc_message_id),
            FOREIGN KEY (tenant_id, thread_id)
                REFERENCES email_threads (tenant_id, thread_id) ON DELETE CASCADE
        )
    """)
    op.execute("CREATE UNIQUE INDEX uq_email_messages_task ON email_messages (tenant_id, task_id) "
               "WHERE task_id IS NOT NULL")
    op.execute("CREATE INDEX ix_email_messages_status "
               "ON email_messages (tenant_id, status, created_at DESC)")
    op.execute("CREATE INDEX ix_email_messages_drafts ON email_messages (created_at) "
               "WHERE status = 'draft'")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS email_messages")
    op.execute("DROP TABLE IF EXISTS email_threads")
    op.execute("DROP TABLE IF EXISTS tenant_email_agent_configs")

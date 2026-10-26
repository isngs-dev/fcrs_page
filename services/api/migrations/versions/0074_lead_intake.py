"""Migration 0074: form & Meta lead intake (Sprint A10)."""
from __future__ import annotations

from alembic import op

revision = "0074"
down_revision = "0073"
branch_labels: tuple[str, ...] = ()
depends_on: tuple[str, ...] = ()

_OLD_SOURCES = "'widget', 'admin_form', 'csv', 'command', 'email_reply', 'call'"


def upgrade() -> None:
    op.execute("""
        CREATE TABLE tenant_lead_intake_configs (
            tenant_id text PRIMARY KEY,
            enabled boolean NOT NULL DEFAULT true,
            trusted_senders text[] NOT NULL DEFAULT '{facebookmail.com}',
            form_consent_wording text NULL,
            call_consent_wording text NULL,
            meta_page_id text NULL,
            meta_page_token_ciphertext text NULL,
            updated_by text NULL,
            updated_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE TABLE meta_page_routes (
            page_id text PRIMARY KEY,
            tenant_id text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE TABLE lead_intake_events (
            tenant_id text NOT NULL,
            intake_id text NOT NULL,
            source text NOT NULL CHECK (source IN ('web_form', 'meta_email', 'meta_api')),
            external_id text NOT NULL,
            status text NOT NULL CHECK (status IN (
                'pending', 'accepted', 'duplicate', 'rejected', 'notice', 'resolved')),
            reason text NULL,
            lead_id text NULL,
            fields jsonb NOT NULL DEFAULT '{}',
            received_at timestamptz NOT NULL DEFAULT now(),
            resolved_by text NULL,
            resolved_at timestamptz NULL,
            PRIMARY KEY (tenant_id, intake_id),
            UNIQUE (tenant_id, source, external_id)
        )
    """)
    op.execute("CREATE INDEX ix_lead_intake_events_queue ON lead_intake_events "
               "(tenant_id, received_at DESC) WHERE status IN ('rejected', 'notice')")
    op.execute("ALTER TABLE contact_consents DROP CONSTRAINT contact_consents_source_check")
    op.execute("ALTER TABLE contact_consents ADD CONSTRAINT contact_consents_source_check "
               f"CHECK (source IN ({_OLD_SOURCES}, 'form', 'meta'))")


def downgrade() -> None:
    op.execute("DELETE FROM contact_consents WHERE source IN ('form', 'meta')")
    op.execute("ALTER TABLE contact_consents DROP CONSTRAINT contact_consents_source_check")
    op.execute("ALTER TABLE contact_consents ADD CONSTRAINT contact_consents_source_check "
               f"CHECK (source IN ({_OLD_SOURCES}))")
    op.execute("DROP TABLE IF EXISTS lead_intake_events")
    op.execute("DROP TABLE IF EXISTS meta_page_routes")
    op.execute("DROP TABLE IF EXISTS tenant_lead_intake_configs")

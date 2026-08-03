"""Migration 0040: accounts, contacts, contact_identities (SR-9.2).

Raw SQL migration (no ORM models / no autogenerate), matching the house
style of 0014/0015.

D10: this revision number was determined by reading the live migration head
(``alembic heads`` / ``migrations/versions/`` listing) immediately before
creating this file. The head was 0039 (``0039_leads_nullable_contact.py`` --
SR-9.1, already shipped), so this migration chains from 0039, not the 0037
the SR-9.2 stub assumed and not the 0039 the spec merely guessed at.

SR-9.2 builds the durable person/company entities the lead funnel's terminal
"converted" stage names but has never had a table for:

- ``accounts``: an optional company a contact may belong to (D3 -- never
  auto-created).
- ``contacts``: the durable person. ``lead_id`` nullable (D2 -- may be
  created with no originating lead), ``account_id`` nullable (D3),
  ``consent`` NOT NULL (D7 -- a Contact never exists without a lawful
  basis). Composite PK/FKs throughout (K4) -- a cross-tenant reference is
  unrepresentable, not merely disallowed.
- ``contact_identities``: the full multi-identity set (D4) -- a person
  accrues many visitor_ids/emails across devices/sessions; the UNIQUE index
  is tenant-scoped (``tenant_id, identity_type, identity_value``) so the
  same identity value under two different tenants never collides.
- ``leads.converted_to_contact_id``: the one-way pointer set by conversion
  (D1). The Lead row is NEVER deleted on conversion -- ``lead_activities``
  has an ON DELETE CASCADE FK to ``leads`` (0015), so a physical delete
  would silently destroy the entire activity/audit timeline. Conversion is
  a tombstone: the Lead stays, gets ``stage='converted'``, and this column
  points at its durable successor.

``ON DELETE SET NULL`` (not CASCADE) on both of ``contacts``' FKs: deleting
an Account must never delete its people, and repeating the K5 cascade
hazard here is deliberately avoided. ``contact_identities`` -> ``contacts``
IS CASCADE, because an identity has no meaning without its contact.
"""
from __future__ import annotations

from alembic import op

revision = "0040"
down_revision = "0039"
branch_labels: tuple[str, ...] = ()
depends_on: tuple[str, ...] = ()


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE accounts (
            tenant_id   text        NOT NULL,
            account_id  text        NOT NULL,
            name        text        NOT NULL,
            domain      text,
            created_at  timestamptz NOT NULL DEFAULT now(),
            updated_at  timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, account_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_accounts_tenant_created "
        "ON accounts (tenant_id, created_at DESC)"
    )

    op.execute(
        """
        CREATE TABLE contacts (
            tenant_id       text        NOT NULL,
            contact_id      text        NOT NULL,
            account_id      text,
            lead_id         text,
            name            text,
            email           text,
            phone           text,
            consent         jsonb       NOT NULL,
            owner_agent_id  text,
            created_at      timestamptz NOT NULL DEFAULT now(),
            updated_at      timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, contact_id),
            FOREIGN KEY (tenant_id, account_id)
                REFERENCES accounts (tenant_id, account_id) ON DELETE SET NULL,
            FOREIGN KEY (tenant_id, lead_id)
                REFERENCES leads (tenant_id, lead_id) ON DELETE SET NULL
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_contacts_tenant_lead "
        "ON contacts (tenant_id, lead_id) WHERE lead_id IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX idx_contacts_tenant_created "
        "ON contacts (tenant_id, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX idx_contacts_tenant_account "
        "ON contacts (tenant_id, account_id)"
    )

    op.execute(
        """
        CREATE TABLE contact_identities (
            tenant_id      text        NOT NULL,
            identity_id    text        NOT NULL,
            contact_id     text        NOT NULL,
            identity_type  text        NOT NULL,
            identity_value text        NOT NULL,
            created_at     timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, identity_id),
            FOREIGN KEY (tenant_id, contact_id)
                REFERENCES contacts (tenant_id, contact_id) ON DELETE CASCADE
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_contact_identities_tenant_type_value "
        "ON contact_identities (tenant_id, identity_type, identity_value)"
    )
    op.execute(
        "CREATE INDEX idx_contact_identities_tenant_contact "
        "ON contact_identities (tenant_id, contact_id)"
    )

    op.execute("ALTER TABLE leads ADD COLUMN converted_to_contact_id text")


def downgrade() -> None:
    op.execute("ALTER TABLE leads DROP COLUMN IF EXISTS converted_to_contact_id")
    op.execute("DROP TABLE IF EXISTS contact_identities")
    op.execute("DROP TABLE IF EXISTS contacts")
    op.execute("DROP TABLE IF EXISTS accounts")

"""Migration 0042: opportunities, tenant_opportunity_configs (SR-9.4).

Raw SQL migration (no ORM models / no autogenerate), matching the house
style of 0014/0015/0040.

D11: this revision number was determined by reading the live migration head
(``migrations/versions/`` listing) immediately before creating this file.
The head was 0041 (``0041_notification_jobs_lead_id.py`` -- SR-9.3, already
shipped), so this migration chains from 0041, not the 0042 the spec merely
guessed at (which happened to be correct this time, but was verified live
rather than assumed -- D11's repeated-defect lesson).

SR-9.4 adds the deal/pipeline entity the SR-9 gap analysis named:

- ``opportunities``: the deal. ``contact_id`` NOT NULL (D6 -- a deal is
  always with a person), ``account_id`` nullable and denormalized/copied
  from the contact at creation time (D6 -- not a live inheritance).
  ``amount numeric(14,2)`` nullable (D7 -- NULL means "not yet quoted",
  never coerced to 0), ``currency`` NOT NULL and stamped from the tenant's
  config at creation (D7 -- a historical record, immutable via PATCH).
  ``stage`` has no DB CHECK -- app-enforced by ``opportunities/pipeline.py``
  (D1), matching the ``lead_activities.type`` precedent (SR-9.2 D4) so the
  stage list can evolve without a migration. No ``win_probability`` column
  -- it is derived at read time from ``(stage, tenant config)`` (D2), never
  stored.
- ``tenant_opportunity_configs``: one row per tenant, currency + per-stage
  win-probabilities together (D10). Resolved by a never-``None``
  ``get_opportunity_config`` (D3) -- an unconfigured tenant deterministically
  gets Pydantic-Settings defaults.

FK notes (M9/D6):
- ``opportunities`` -> ``contacts`` is NOT ``ON DELETE SET NULL`` and NOT
  CASCADE (contact_id is NOT NULL) -- deleting a contact with open deals
  raises an FK violation, the honest outcome (no contact-DELETE endpoint
  exists anyway; SR-1.7 owns GDPR erasure design).
- ``opportunities`` -> ``accounts`` IS ``ON DELETE SET NULL`` (account_id is
  nullable) -- deleting a company must never delete its deals, matching the
  K5-lesson precedent set by migration 0040.
"""
from __future__ import annotations

from alembic import op

revision = "0042"
down_revision = "0041"
branch_labels: tuple[str, ...] = ()
depends_on: tuple[str, ...] = ()


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE opportunities (
            tenant_id            text        NOT NULL,
            opportunity_id       text        NOT NULL,
            contact_id           text        NOT NULL,
            account_id           text,
            name                 text        NOT NULL,
            amount               numeric(14,2),
            currency             text        NOT NULL,
            stage                text        NOT NULL DEFAULT 'prospecting',
            expected_close_date  date,
            closed_at            timestamptz,
            close_reason         text,
            owner_agent_id       text,
            created_at           timestamptz NOT NULL DEFAULT now(),
            updated_at           timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, opportunity_id),
            FOREIGN KEY (tenant_id, contact_id)
                REFERENCES contacts (tenant_id, contact_id),
            FOREIGN KEY (tenant_id, account_id)
                REFERENCES accounts (tenant_id, account_id) ON DELETE SET NULL,
            CONSTRAINT ck_opportunities_amount_non_negative
                CHECK (amount IS NULL OR amount >= 0)
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_opportunities_tenant_created "
        "ON opportunities (tenant_id, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX idx_opportunities_tenant_stage "
        "ON opportunities (tenant_id, stage)"
    )
    op.execute(
        "CREATE INDEX idx_opportunities_tenant_contact "
        "ON opportunities (tenant_id, contact_id)"
    )
    op.execute(
        "CREATE INDEX idx_opportunities_tenant_account "
        "ON opportunities (tenant_id, account_id)"
    )

    op.execute(
        """
        CREATE TABLE tenant_opportunity_configs (
            tenant_id           text        PRIMARY KEY,
            currency            text        NOT NULL,
            stage_probabilities jsonb       NOT NULL,
            created_at          timestamptz NOT NULL DEFAULT now(),
            updated_at          timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_tenant_opportunity_configs_currency
                CHECK (currency ~ '^[A-Z]{3}$')
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS tenant_opportunity_configs")
    op.execute("DROP TABLE IF EXISTS opportunities")

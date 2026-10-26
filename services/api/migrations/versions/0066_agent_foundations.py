"""Migration 0066: agent foundations (Sprint A1).

Outreach settings + platform kill switch, per-channel consent records,
suppression list, the transactional outbox (``agent_events``), campaigns /
plans / tasks, agent run traces + tool calls, usage costs, the audit hash
chain columns, ``contacts.timezone``, and canonical email/phone identities
(``contact_identities``: email lower-cased, ``phone`` identities in E.164).

Fails loudly (does not guess) when lower-casing would make two existing email
identities collide inside one tenant -- an operator must merge those contacts
first. Phones that can't be normalised, or already belong to another contact,
are skipped and counted.
"""
from __future__ import annotations

from uuid import uuid4

import sqlalchemy as sa
from alembic import op
from common.phone import to_e164

revision = "0066"
down_revision = "0065"
branch_labels: tuple[str, ...] = ()
depends_on: tuple[str, ...] = ()


_TABLES = [
    """
    CREATE TABLE agent_settings (
        tenant_id text PRIMARY KEY,
        enabled_channels text[] NOT NULL DEFAULT '{}',
        email_mode text NOT NULL DEFAULT 'draft_for_review'
            CHECK (email_mode IN ('auto_send', 'draft_for_review')),
        outreach_paused boolean NOT NULL DEFAULT false,
        region_profile text NOT NULL DEFAULT 'US',
        call_window_start time NOT NULL DEFAULT '09:00',
        call_window_end time NOT NULL DEFAULT '20:00',
        max_touches_per_contact int NOT NULL DEFAULT 3
            CHECK (max_touches_per_contact BETWEEN 1 AND 10),
        max_call_attempts int NOT NULL DEFAULT 2 CHECK (max_call_attempts BETWEEN 1 AND 5),
        min_hours_between_touches int NOT NULL DEFAULT 48
            CHECK (min_hours_between_touches BETWEEN 1 AND 720),
        first_inbound_email_delay_minutes int NOT NULL DEFAULT 5
            CHECK (first_inbound_email_delay_minutes BETWEEN 0 AND 1440),
        monthly_cost_cap_micros bigint NULL,
        physical_address text NULL,
        updated_at timestamptz NOT NULL DEFAULT now(),
        updated_by text NULL,
        CHECK (call_window_start < call_window_end)
    )
    """,
    """
    CREATE TABLE platform_flags (
        key text PRIMARY KEY,
        value jsonb NOT NULL,
        updated_at timestamptz NOT NULL DEFAULT now(),
        updated_by text NULL
    )
    """,
    "INSERT INTO platform_flags (key, value) VALUES ('outreach_paused', 'false')",
    "ALTER TABLE contacts ADD COLUMN timezone text NULL",
    """
    CREATE TABLE contact_consents (
        tenant_id text NOT NULL,
        consent_id text NOT NULL,
        contact_id text NOT NULL,
        channel text NOT NULL CHECK (channel IN ('email', 'call', 'sms')),
        status text NOT NULL CHECK (status IN ('granted', 'revoked')),
        basis text NOT NULL CHECK (basis IN (
            'express_consent', 'express_written_consent', 'documented_lawful_basis')),
        source text NOT NULL CHECK (source IN (
            'widget', 'admin_form', 'csv', 'command', 'email_reply', 'call')),
        wording text NULL,
        wording_version text NULL,
        evidence jsonb NOT NULL DEFAULT '{}',
        recorded_by text NULL,
        recorded_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (tenant_id, consent_id),
        FOREIGN KEY (tenant_id, contact_id)
            REFERENCES contacts (tenant_id, contact_id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX ix_contact_consents_latest "
    "ON contact_consents (tenant_id, contact_id, channel, recorded_at DESC)",
    """
    CREATE TABLE suppression_list (
        tenant_id text NOT NULL,
        suppression_id text NOT NULL,
        channel text NOT NULL CHECK (channel IN ('email', 'call', 'sms', 'all')),
        value_normalised text NOT NULL,
        reason text NOT NULL CHECK (reason IN (
            'unsubscribe', 'stop', 'dnc_request', 'bounce', 'complaint',
            'wrong_number', 'manual', 'registry')),
        source text NOT NULL,
        created_by text NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (tenant_id, suppression_id),
        UNIQUE (tenant_id, channel, value_normalised)
    )
    """,
    """
    CREATE TABLE agent_events (
        tenant_id text NOT NULL,
        event_id text NOT NULL,
        kind text NOT NULL,
        payload jsonb NOT NULL,
        status text NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending', 'processing', 'done', 'failed')),
        attempts int NOT NULL DEFAULT 0,
        last_error text NULL,
        available_at timestamptz NOT NULL DEFAULT now(),
        created_at timestamptz NOT NULL DEFAULT now(),
        processed_at timestamptz NULL,
        correlation_id text NULL,
        PRIMARY KEY (tenant_id, event_id)
    )
    """,
    "CREATE INDEX ix_agent_events_due ON agent_events (available_at) WHERE status = 'pending'",
    "CREATE INDEX ix_agent_events_processing ON agent_events (processed_at) "
    "WHERE status = 'processing'",
    """
    CREATE TABLE outreach_campaigns (
        tenant_id text NOT NULL,
        campaign_id text NOT NULL,
        name text NOT NULL,
        kind text NOT NULL CHECK (kind IN ('inbound_auto', 'outbound_manual', 'outbound_csv')),
        goal text NULL,
        status text NOT NULL DEFAULT 'active'
            CHECK (status IN ('active', 'paused', 'completed', 'cancelled')),
        created_by text NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (tenant_id, campaign_id)
    )
    """,
    """
    CREATE TABLE outreach_plans (
        tenant_id text NOT NULL,
        plan_id text NOT NULL,
        campaign_id text NOT NULL,
        contact_id text NOT NULL,
        status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'stopped', 'completed')),
        stop_reason text NULL,
        plan jsonb NOT NULL,
        policy_notes jsonb NOT NULL DEFAULT '[]',
        created_by text NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (tenant_id, plan_id),
        FOREIGN KEY (tenant_id, campaign_id) REFERENCES outreach_campaigns (tenant_id, campaign_id),
        FOREIGN KEY (tenant_id, contact_id)
            REFERENCES contacts (tenant_id, contact_id) ON DELETE CASCADE
    )
    """,
    "CREATE UNIQUE INDEX uq_outreach_plans_active_contact "
    "ON outreach_plans (tenant_id, contact_id) WHERE status = 'active'",
    """
    CREATE TABLE outreach_tasks (
        tenant_id text NOT NULL,
        task_id text NOT NULL,
        plan_id text NOT NULL,
        step_index int NOT NULL,
        channel text NOT NULL CHECK (channel IN ('email', 'call', 'sms')),
        goal text NOT NULL,
        intent text NOT NULL DEFAULT '',
        scheduled_at timestamptz NOT NULL,
        status text NOT NULL DEFAULT 'scheduled' CHECK (status IN (
            'pending', 'scheduled', 'in_progress', 'completed', 'failed', 'cancelled',
            'escalated')),
        attempts int NOT NULL DEFAULT 0,
        max_attempts int NOT NULL DEFAULT 1,
        claimed_at timestamptz NULL,
        completed_at timestamptz NULL,
        result jsonb NULL,
        last_error text NULL,
        idempotency_key text NOT NULL,
        PRIMARY KEY (tenant_id, task_id),
        UNIQUE (tenant_id, idempotency_key),
        FOREIGN KEY (tenant_id, plan_id)
            REFERENCES outreach_plans (tenant_id, plan_id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX ix_outreach_tasks_due ON outreach_tasks (scheduled_at) "
    "WHERE status = 'scheduled'",
    "CREATE INDEX ix_outreach_tasks_in_progress ON outreach_tasks (claimed_at) "
    "WHERE status = 'in_progress'",
    "CREATE INDEX ix_outreach_tasks_plan ON outreach_tasks (tenant_id, plan_id, step_index)",
    """
    CREATE TABLE agent_runs (
        tenant_id text NOT NULL,
        run_id text NOT NULL,
        correlation_id text NULL,
        trigger_event_id text NULL,
        agent text NOT NULL CHECK (agent IN ('orchestrator', 'email', 'call', 'chat')),
        model text NULL,
        status text NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'ok', 'failed')),
        started_at timestamptz NOT NULL DEFAULT now(),
        ended_at timestamptz NULL,
        tokens_in int NOT NULL DEFAULT 0,
        tokens_out int NOT NULL DEFAULT 0,
        PRIMARY KEY (tenant_id, run_id)
    )
    """,
    """
    CREATE TABLE agent_tool_calls (
        tenant_id text NOT NULL,
        run_id text NOT NULL,
        seq int NOT NULL,
        tool text NOT NULL,
        args_redacted jsonb NOT NULL,
        result_summary jsonb NULL,
        ok boolean NOT NULL,
        error_code text NULL,
        audit_event_id text NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (tenant_id, run_id, seq),
        FOREIGN KEY (tenant_id, run_id) REFERENCES agent_runs (tenant_id, run_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE usage_costs (
        tenant_id text NOT NULL,
        cost_id text NOT NULL,
        kind text NOT NULL CHECK (kind IN ('llm', 'asr', 'tts', 'telephony', 'email')),
        units numeric NOT NULL,
        unit text NOT NULL,
        cost_micros bigint NOT NULL,
        ref_type text NULL,
        ref_id text NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (tenant_id, cost_id)
    )
    """,
    "CREATE INDEX ix_usage_costs_month ON usage_costs (tenant_id, created_at)",
    "ALTER TABLE audit_events ADD COLUMN prev_hash text NULL, "
    "ADD COLUMN row_hash text NULL, ADD COLUMN chain_seq bigint NULL",
    "CREATE INDEX ix_audit_events_unsealed ON audit_events (created_at) WHERE row_hash IS NULL",
    "CREATE INDEX ix_audit_events_chain ON audit_events (tenant_id, chain_seq) "
    "WHERE chain_seq IS NOT NULL",
    """
    CREATE TABLE audit_chain_heads (
        tenant_id text PRIMARY KEY,
        last_hash text NOT NULL,
        last_seq bigint NOT NULL,
        updated_at timestamptz NOT NULL DEFAULT now()
    )
    """,
]


def _normalise_identities() -> None:
    bind = op.get_bind()
    collisions = bind.execute(sa.text(
        "SELECT count(*) FROM ("
        "  SELECT tenant_id, lower(btrim(identity_value)) FROM contact_identities "
        "  WHERE identity_type = 'email' GROUP BY 1, 2 HAVING count(*) > 1"
        ") AS dup"
    )).scalar_one()
    if collisions:
        raise RuntimeError(
            f"0066: {collisions} email identity value(s) collide once lower-cased within a "
            "tenant. Merge those contacts, then re-run the migration."
        )
    bind.execute(sa.text(
        "UPDATE contact_identities SET identity_value = lower(btrim(identity_value)) "
        "WHERE identity_type = 'email' AND identity_value <> lower(btrim(identity_value))"
    ))

    rows = bind.execute(sa.text(
        "SELECT tenant_id, contact_id, phone FROM contacts WHERE phone IS NOT NULL AND phone <> ''"
    )).all()
    added = undialable = taken = 0
    for tenant_id, contact_id, phone in rows:
        e164 = to_e164(phone)
        if e164 is None:
            undialable += 1
            continue
        result = bind.execute(
            sa.text(
                "INSERT INTO contact_identities "
                "(tenant_id, identity_id, contact_id, identity_type, identity_value) "
                "VALUES (:t, :i, :c, 'phone', :v) "
                "ON CONFLICT (tenant_id, identity_type, identity_value) DO NOTHING"
            ),
            {"t": tenant_id, "i": uuid4().hex, "c": contact_id, "v": e164},
        )
        if result.rowcount:
            added += 1
        else:
            taken += 1
    print(  # noqa: T201 -- migration report for the operator
        f"0066: phone identities added={added} undialable_skipped={undialable} "
        f"already_claimed_skipped={taken}"
    )


def upgrade() -> None:
    for statement in _TABLES:
        op.execute(statement)
    _normalise_identities()


def downgrade() -> None:
    # Email identities stay lower-cased (not reversible: the original casing is gone).
    op.execute("DELETE FROM contact_identities WHERE identity_type = 'phone'")
    op.execute("DROP TABLE IF EXISTS audit_chain_heads")
    op.execute("DROP INDEX IF EXISTS ix_audit_events_chain")
    op.execute("DROP INDEX IF EXISTS ix_audit_events_unsealed")
    op.execute(
        "ALTER TABLE audit_events DROP COLUMN IF EXISTS chain_seq, "
        "DROP COLUMN IF EXISTS row_hash, DROP COLUMN IF EXISTS prev_hash"
    )
    for table in (
        "usage_costs", "agent_tool_calls", "agent_runs", "outreach_tasks", "outreach_plans",
        "outreach_campaigns", "agent_events", "suppression_list", "contact_consents",
        "platform_flags", "agent_settings",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table}")
    op.execute("ALTER TABLE contacts DROP COLUMN IF EXISTS timezone")

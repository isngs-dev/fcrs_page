"""Migration 0071: Calling Agent, outbound (Sprint A6).

Per-tenant Plivo call config (auth token AES-GCM encrypted, no platform
default), call sessions, and transcript turns with per-turn latency.
"""
from __future__ import annotations

from alembic import op

revision = "0071"
down_revision = "0070"
branch_labels: tuple[str, ...] = ()
depends_on: tuple[str, ...] = ()


def upgrade() -> None:
    op.execute("""
        CREATE TABLE tenant_call_agent_configs (
            tenant_id text PRIMARY KEY,
            enabled boolean NOT NULL DEFAULT true,
            plivo_auth_id text NOT NULL,
            plivo_auth_token_ciphertext text NOT NULL,
            from_number text NOT NULL,
            transfer_number text NULL,
            company_name text NOT NULL,
            voice text NOT NULL DEFAULT 'Polly.Joanna-Neural',
            voice_model text NULL,
            voicemail_policy text NOT NULL DEFAULT 'hangup'
                CHECK (voicemail_policy IN ('hangup', 'leave_message')),
            voicemail_message text NULL,
            max_call_seconds int NOT NULL DEFAULT 300 CHECK (max_call_seconds BETWEEN 60 AND 1800),
            max_concurrent_calls int NOT NULL DEFAULT 2
                CHECK (max_concurrent_calls BETWEEN 1 AND 20),
            recording_enabled boolean NOT NULL DEFAULT true,
            updated_at timestamptz NOT NULL DEFAULT now(),
            updated_by text NULL
        )
    """)
    op.execute("""
        CREATE TABLE call_sessions (
            tenant_id text NOT NULL,
            call_id text NOT NULL,
            direction text NOT NULL CHECK (direction IN ('outbound', 'inbound')),
            task_id text NULL,
            plan_id text NULL,
            contact_id text NULL,
            from_e164 text NOT NULL,
            to_e164 text NOT NULL,
            provider_request_uuid text NULL,
            provider_call_uuid text NULL,
            status text NOT NULL DEFAULT 'dialing' CHECK (status IN (
                'dialing', 'in_progress', 'transferring', 'completed', 'no_answer',
                'voicemail', 'failed')),
            outcome text NULL CHECK (outcome IN (
                'booked', 'interested', 'callback', 'not_interested', 'no_answer', 'voicemail',
                'wrong_number', 'opted_out', 'escalated', 'failed', 'completed')),
            amd_result text NULL,
            recording_consent boolean NOT NULL DEFAULT true,
            recording_id text NULL,
            recording_url text NULL,
            transfer_reason text NULL,
            summary text NULL,
            sentiment text NULL,
            session jsonb NOT NULL DEFAULT '{}',
            created_at timestamptz NOT NULL DEFAULT now(),
            answered_at timestamptz NULL,
            ended_at timestamptz NULL,
            duration_s int NULL,
            PRIMARY KEY (tenant_id, call_id)
        )
    """)
    op.execute("CREATE INDEX ix_call_sessions_active ON call_sessions (tenant_id, created_at) "
               "WHERE status IN ('dialing', 'in_progress', 'transferring')")
    op.execute("CREATE INDEX ix_call_sessions_recent ON call_sessions (tenant_id, created_at DESC)")
    op.execute("""
        CREATE TABLE call_transcript_turns (
            tenant_id text NOT NULL,
            call_id text NOT NULL,
            turn_index int NOT NULL,
            speaker text NOT NULL CHECK (speaker IN ('rebecca', 'caller', 'system')),
            text text NOT NULL,
            latency_ms int NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, call_id, turn_index),
            FOREIGN KEY (tenant_id, call_id)
                REFERENCES call_sessions (tenant_id, call_id) ON DELETE CASCADE
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS call_transcript_turns")
    op.execute("DROP TABLE IF EXISTS call_sessions")
    op.execute("DROP TABLE IF EXISTS tenant_call_agent_configs")

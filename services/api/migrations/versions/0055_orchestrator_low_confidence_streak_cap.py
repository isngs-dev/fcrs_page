"""Migration 0055: add tenant_orchestrator_configs.low_confidence_streak_cap.

Raw SQL migration (no ORM models / no autogenerate) -- same style as 0027.

Adds a nullable per-tenant ``low_confidence_streak_cap integer`` column to the
EXISTING ``tenant_orchestrator_configs`` table. This backs the "repeated
low-confidence turns" early-escalation feature: when the last N consecutive
assistant turns all landed in the clarify/escalate band (never a real
"answer"), the orchestrator escalates immediately instead of waiting for the
generic turn_cap -- the same class of orchestration policy as
``answer_threshold``/``escalate_threshold``/``turn_cap``, so it belongs on the
same row. A CHECK constraint enforces
``low_confidence_streak_cap IS NULL OR low_confidence_streak_cap >= 1``
(defense-in-depth over ``upsert_orchestrator_config``'s own validation).
Nullable so existing rows resolve to
``settings.orchestrator_default_low_confidence_streak_cap`` at read time
(``get_orchestrator_config``). Additive + nullable + CHECK-guarded, mirroring
0027's low-risk shape exactly.
"""
from __future__ import annotations

from alembic import op

revision = "0055"
down_revision = "0054"
branch_labels: tuple[str, ...] = ()
depends_on: tuple[str, ...] = ()


def upgrade() -> None:
    op.execute(
        "ALTER TABLE tenant_orchestrator_configs ADD COLUMN low_confidence_streak_cap integer"
    )
    op.execute(
        "ALTER TABLE tenant_orchestrator_configs "
        "ADD CONSTRAINT ck_orchestrator_low_confidence_streak_cap "
        "CHECK (low_confidence_streak_cap IS NULL OR low_confidence_streak_cap >= 1)"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE tenant_orchestrator_configs "
        "DROP CONSTRAINT IF EXISTS ck_orchestrator_low_confidence_streak_cap"
    )
    op.execute("ALTER TABLE tenant_orchestrator_configs DROP COLUMN low_confidence_streak_cap")

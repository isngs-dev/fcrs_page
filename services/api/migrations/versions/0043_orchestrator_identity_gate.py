"""Migration 0043: add tenant_orchestrator_configs.identity_gate_enabled (SR-14).

Raw SQL migration (no ORM models / no autogenerate) -- same style as 0027/0041.

D11: this revision number was determined by reading the live migration head
(``alembic heads``) immediately before creating this file. The head was 0042
(``0042_opportunities_tenant_opportunity_configs.py`` -- SR-9.4, already
shipped), so this migration chains from 0042, not the 0041 the sprint spec
guessed at (the spec was written before SR-9.4/SR-9.5 landed) -- verified
live rather than assumed, per the repeated house rule.

SR-14 (conversation-start identity gate) needs a per-tenant, default-OFF
switch (D9) for whether the bot asks for name + email on its first reply.
Adds a nullable ``identity_gate_enabled boolean`` column to the EXISTING
``tenant_orchestrator_configs`` table (S10.2/S10.4) -- the identity-gate
toggle is the same class of orchestration policy as ``answer_threshold``/
``escalate_threshold``/``turn_cap``, so it belongs on the same row rather
than a new table. No CHECK constraint -- a boolean has no invalid range to
guard (unlike ``turn_cap``'s ``>= 1`` invariant), mirroring 0040/0041's
CHECK-free shape rather than 0027's CHECK-guarded one. Nullable so existing
rows resolve to ``False`` (OFF) at read time (``get_orchestrator_config``) --
additive + nullable, mirroring 0027/0041's low-risk shape. Default OFF means
no existing tenant's behavior changes until the flag is explicitly set.
"""
from __future__ import annotations

from alembic import op

revision = "0043"
down_revision = "0042"
branch_labels: tuple[str, ...] = ()
depends_on: tuple[str, ...] = ()


def upgrade() -> None:
    op.execute(
        "ALTER TABLE tenant_orchestrator_configs ADD COLUMN identity_gate_enabled boolean"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE tenant_orchestrator_configs DROP COLUMN IF EXISTS identity_gate_enabled"
    )

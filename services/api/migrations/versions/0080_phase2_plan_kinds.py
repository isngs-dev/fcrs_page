"""Migration 0080: the Phase 2 plan kinds (stored on the campaign) -- no-show recovery (2.1),
after the meeting (2.2) and nurture (2.3) -- in one migration, so the phase needs one
migration step on deploy."""
from __future__ import annotations

from alembic import op

revision = "0080"
down_revision = "0079"
branch_labels: tuple[str, ...] = ()
depends_on: tuple[str, ...] = ()

_OLD = "'inbound_auto', 'outbound_manual', 'outbound_csv'"
_PHASE2 = "'no_show_recovery', 'after_meeting', 'nurture'"
_NEW = f"{_OLD}, {_PHASE2}"


def upgrade() -> None:
    op.execute("ALTER TABLE outreach_campaigns DROP CONSTRAINT outreach_campaigns_kind_check")
    op.execute("ALTER TABLE outreach_campaigns ADD CONSTRAINT outreach_campaigns_kind_check "
               f"CHECK (kind IN ({_NEW}))")


def downgrade() -> None:
    # Fixed constants, no input.
    op.execute("DELETE FROM outreach_plans WHERE (tenant_id, campaign_id) IN (SELECT tenant_id, "  # noqa: S608
               f"campaign_id FROM outreach_campaigns WHERE kind IN ({_PHASE2}))")
    op.execute(f"DELETE FROM outreach_campaigns WHERE kind IN ({_PHASE2})")  # noqa: S608
    op.execute("ALTER TABLE outreach_campaigns DROP CONSTRAINT outreach_campaigns_kind_check")
    op.execute("ALTER TABLE outreach_campaigns ADD CONSTRAINT outreach_campaigns_kind_check "
               f"CHECK (kind IN ({_OLD}))")

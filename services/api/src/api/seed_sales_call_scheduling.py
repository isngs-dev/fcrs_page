"""One-off, idempotent script: seed native scheduling availability + turn_cap
for the sales-call-booking flow fix (Tier 1, data seeding steps 9-12).

Usage::

    python -m api.seed_sales_call_scheduling [--tenant-slug SLUG] [--yes]

Connects via ``DATABASE_URL_DIRECT`` (fallback ``DATABASE_URL``), exactly like
``api.seed``. Goes through the SAME repository functions the admin API routes
use (``api.scheduling.repository.upsert_availability``,
``api.scheduling.calendar_config_repository.{get,upsert}_calendar_config``,
``api.orchestrator.config_repository.{get,upsert}_orchestrator_config``) --
never hand-written SQL that bypasses their validation -- via a synthetic
``AuthClaims(role=CLIENT_ADMIN)`` for the resolved tenant, the same shape a
real admin request carries. This mirrors calling the real
``PUT /admin/schedule/availability`` / ``PUT /admin/settings`` endpoints
without needing a running HTTP server in a bare dev environment.

Tenant resolution (deliberately conservative -- never guesses):
1. ``--tenant-slug`` if given.
2. ``SEED_TENANT_SLUG`` env var (the same var ``api.seed`` uses) if set.
3. Otherwise, if exactly ONE row exists in ``tenants``, uses it.
4. Otherwise (zero or multiple tenants and no slug given), STOPS and prints
   every tenant's id/slug/name -- it never guesses which one is "the" tenant.

What it does, in order (each step is idempotent -- safe to re-run):
1. Checks ``tenant_calendar_configs`` for ``provider='calendly' AND
   enabled=true``. If found, disables it (``enabled=False``) via
   ``upsert_calendar_config``, preserving every other field (credentials,
   calendar_id, scheduling_url) untouched -- the native calendar flow must
   win, but nothing else about that row is touched.
2. Upserts ``availability``: timezone UTC, Mon-Fri 09:00-18:00, 30-minute
   slots, 0 buffer -- validated through the SAME Pydantic models
   ``admin_routes.py`` uses (``AvailabilityUpsertRequest``/``RulesPayload``)
   before the repository call, so this can never persist a shape the real
   endpoint would have rejected with a 422.
3. Sets ``turn_cap = 2`` on ``tenant_orchestrator_configs``, preserving the
   tenant's existing ``answer_threshold``/``escalate_threshold``/
   ``identity_gate_enabled`` (reads the current config first -- same
   preserve-don't-clobber pattern as ``settings_routes.py#_put_settings``).

Prints a summary of what changed (never prints credentials/secrets).
"""
from __future__ import annotations

import argparse
import asyncio
import os

from common.auth import AuthClaims, Role
from common.db import Database

from api.orchestrator.config_repository import get_orchestrator_config, upsert_orchestrator_config
from api.scheduling.admin_routes import AvailabilityUpsertRequest, RulesPayload
from api.scheduling.calendar_config_repository import get_calendar_config, upsert_calendar_config
from api.scheduling.repository import upsert_availability

_TARGET_RULES = RulesPayload(
    slot_minutes=30,
    buffer_minutes=0,
    weekly_hours={
        "mon": [["09:00", "18:00"]],
        "tue": [["09:00", "18:00"]],
        "wed": [["09:00", "18:00"]],
        "thu": [["09:00", "18:00"]],
        "fri": [["09:00", "18:00"]],
    },
)
_TARGET_TIMEZONE = "UTC"
_TARGET_TURN_CAP = 2


async def _resolve_tenant(db: Database, tenant_slug: str | None) -> str:
    if tenant_slug:
        tenant_id = await db.fetchval("SELECT id FROM tenants WHERE slug = $1", tenant_slug)
        if tenant_id is None:
            raise SystemExit(f"No tenant with slug={tenant_slug!r} found.")
        return str(tenant_id)

    env_slug = os.environ.get("SEED_TENANT_SLUG", "").strip()
    if env_slug:
        tenant_id = await db.fetchval("SELECT id FROM tenants WHERE slug = $1", env_slug)
        if tenant_id is None:
            raise SystemExit(f"SEED_TENANT_SLUG={env_slug!r} does not match any tenant.")
        return str(tenant_id)

    rows = await db.fetch("SELECT id, slug, name FROM tenants ORDER BY name")
    if len(rows) == 1:
        return str(rows[0]["id"])

    if len(rows) == 0:
        raise SystemExit("No tenants exist in this database -- nothing to seed.")

    print("Multiple tenants exist and none was specified -- refusing to guess:")
    for row in rows:
        print(f"  id={row['id']}  slug={row['slug']}  name={row['name']}")
    raise SystemExit(
        "Re-run with --tenant-slug <slug> (or set SEED_TENANT_SLUG) to pick one."
    )


async def _seed(tenant_slug: str | None) -> None:
    dsn = os.environ.get("DATABASE_URL_DIRECT") or os.environ.get("DATABASE_URL")
    if not dsn:
        raise SystemExit("Set DATABASE_URL_DIRECT or DATABASE_URL to run this script.")

    db = await Database.connect(dsn, min_size=1, max_size=2)
    try:
        tenant_id = await _resolve_tenant(db, tenant_slug)
        claims = AuthClaims(subject="seed-script", role=Role.CLIENT_ADMIN, tenant_id=tenant_id)

        print(f"=== Seeding sales-call scheduling for tenant_id={tenant_id} ===")

        # -- 1. Disable a pre-existing enabled Calendly config, if any -------
        calendar_config = await get_calendar_config(db, claims)
        calendly_enabled = (
            calendar_config is not None
            and calendar_config.provider == "calendly"
            and calendar_config.enabled
        )
        if calendly_enabled and calendar_config is not None:
            await upsert_calendar_config(
                db,
                claims,
                provider=calendar_config.provider,
                calendar_id=calendar_config.calendar_id,
                credentials=calendar_config.credentials,
                busy=calendar_config.busy,
                enabled=False,
                scheduling_url=calendar_config.scheduling_url,
            )
            print(
                "  tenant_calendar_configs: found enabled Calendly config "
                "-> disabled (enabled=false)."
            )
        elif calendar_config is not None and calendar_config.provider == "calendly":
            print("  tenant_calendar_configs: Calendly config already disabled -- no change.")
        else:
            print("  tenant_calendar_configs: no Calendly config found -- nothing to disable.")

        # -- 2. Upsert native availability -----------------------------------
        # Validated through the same Pydantic request model the real
        # PUT /admin/schedule/availability endpoint validates against.
        validated = AvailabilityUpsertRequest(timezone=_TARGET_TIMEZONE, rules=_TARGET_RULES)
        availability = await upsert_availability(
            db, claims, timezone=validated.timezone, rules=validated.rules.model_dump()
        )
        print(
            "  availability: upserted "
            f"timezone={availability.timezone} rules={availability.rules}"
        )

        # -- 3. Set turn_cap=2, preserving existing thresholds/gate ----------
        current = await get_orchestrator_config(db, claims)
        await upsert_orchestrator_config(
            db,
            claims,
            answer_threshold=current.answer_threshold,
            escalate_threshold=current.escalate_threshold,
            turn_cap=_TARGET_TURN_CAP,
            identity_gate_enabled=current.identity_gate_enabled,
        )
        print(
            f"  tenant_orchestrator_configs: turn_cap -> {_TARGET_TURN_CAP} "
            f"(answer_threshold={current.answer_threshold}, "
            f"escalate_threshold={current.escalate_threshold}, "
            f"identity_gate_enabled={current.identity_gate_enabled} preserved)"
        )

        print("=== Done ===")
    finally:
        await db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tenant-slug",
        default=None,
        help="Tenant slug to seed. Defaults to SEED_TENANT_SLUG env var, or the "
        "single tenant row if exactly one exists.",
    )
    args = parser.parse_args()
    asyncio.run(_seed(args.tenant_slug))


if __name__ == "__main__":
    main()

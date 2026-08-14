"""One-off, idempotent script: backfill email qualification for every
EXISTING 'captured' lead that has never been classified.

Usage::

    python -m api.reclassify_captured_leads \
        [--tenant-slug SLUG] [--batch-size N] [--pause-seconds N]

Connects via ``DATABASE_URL_DIRECT`` (fallback ``DATABASE_URL``), exactly
like ``api.seed_sales_call_scheduling`` -- same conservative tenant
resolution (``--tenant-slug``, else ``SEED_TENANT_SLUG``, else the single
tenant row if exactly one exists, else refuse to guess and list every
tenant).

New leads captured after this feature shipped are classified automatically
(``api.leads.routes``/``api.scheduling.routes`` enqueue
``api.leads.tasks.classify_lead_email`` on capture) -- this script exists
only to catch up the backlog of leads that were already sitting in
``captured`` before that wiring existed. It enqueues the SAME Celery task
real captures use (never a parallel/hand-written classification path here),
so a backfilled lead is classified identically to one classified live.

Idempotent and safe to re-run: only leads with ``stage='captured' AND
email_verdict IS NULL`` are candidates, and ``classify_lead_email`` itself
is a pure function of the email (re-running it is always safe). A lead with
``email IS NULL`` (an anonymous SR-9.1 booking-created lead) is skipped here
too -- same as the live task -- never enqueued for a lead that has nothing
to classify.

Enqueues in small batches with a pause between them (default: 25 leads,
2 seconds) so a large backlog never storms the DNS resolver or the Celery
broker -- this is deliberately NOT a single unbounded enqueue loop.
"""
from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from common.auth import AuthClaims, Role
from common.db import Database
from dotenv import load_dotenv

# Load .env from the repo root regardless of CWD (mirrors migrations/env.py,
# api.seed, and api.seed_sales_call_scheduling, which all load the same file
# so each works standalone without requiring the caller to export env vars
# manually first) -- MUST run before the api.leads.tasks import below, since
# that import eagerly constructs the Celery app (api.tasks.celery_app reads
# REDIS_URL/CELERY_BROKER_URL at import time, unlike this script's other
# api.* imports, which are lazy about settings).
load_dotenv(Path(__file__).resolve().parents[4] / ".env")

from api.leads.repository import list_leads  # noqa: E402
from api.leads.tasks import classify_lead_email  # noqa: E402

_DEFAULT_BATCH_SIZE = 25
_DEFAULT_PAUSE_SECONDS = 2.0
_PAGE_SIZE = 200


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
        raise SystemExit("No tenants exist in this database -- nothing to backfill.")

    print("Multiple tenants exist and none was specified -- refusing to guess:")
    for row in rows:
        print(f"  id={row['id']}  slug={row['slug']}  name={row['name']}")
    raise SystemExit(
        "Re-run with --tenant-slug <slug> (or set SEED_TENANT_SLUG) to pick one."
    )


async def _backfill(
    tenant_slug: str | None, *, batch_size: int, pause_seconds: float,
) -> None:
    dsn = os.environ.get("DATABASE_URL_DIRECT") or os.environ.get("DATABASE_URL")
    if not dsn:
        raise SystemExit("Set DATABASE_URL_DIRECT or DATABASE_URL to run this script.")

    db = await Database.connect(dsn, min_size=1, max_size=2)
    try:
        tenant_id = await _resolve_tenant(db, tenant_slug)
        claims = AuthClaims(
            subject="system:lead-qualification-backfill",
            role=Role.CLIENT_ADMIN,
            tenant_id=tenant_id,
        )

        print(f"=== Backfilling email qualification for tenant_id={tenant_id} ===")

        candidate_lead_ids: list[str] = []
        offset = 0
        while True:
            page, total = await list_leads(
                db, claims, stage="captured", limit=_PAGE_SIZE, offset=offset,
            )
            for lead in page:
                if lead.email is not None and lead.email_verdict is None:
                    candidate_lead_ids.append(lead.lead_id)
            offset += _PAGE_SIZE
            if offset >= total:
                break

        print(f"  found {len(candidate_lead_ids)} unclassified captured lead(s)")

        enqueued = 0
        for i, lead_id in enumerate(candidate_lead_ids):
            classify_lead_email.delay(tenant_id=tenant_id, lead_id=lead_id)
            enqueued += 1
            is_last = i == len(candidate_lead_ids) - 1
            if not is_last and enqueued % batch_size == 0:
                total = len(candidate_lead_ids)
                print(f"  enqueued {enqueued}/{total} -- pausing {pause_seconds}s")
                await asyncio.sleep(pause_seconds)

        print(f"=== Done -- enqueued {enqueued} lead(s) for classification ===")
    finally:
        await db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tenant-slug",
        default=None,
        help="Tenant slug to backfill. Defaults to SEED_TENANT_SLUG env var, or the "
        "single tenant row if exactly one exists.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=_DEFAULT_BATCH_SIZE,
        help=f"Leads to enqueue before pausing (default: {_DEFAULT_BATCH_SIZE}).",
    )
    parser.add_argument(
        "--pause-seconds",
        type=float,
        default=_DEFAULT_PAUSE_SECONDS,
        help=f"Seconds to pause between batches (default: {_DEFAULT_PAUSE_SECONDS}).",
    )
    args = parser.parse_args()
    asyncio.run(
        _backfill(args.tenant_slug, batch_size=args.batch_size, pause_seconds=args.pause_seconds)
    )


if __name__ == "__main__":
    main()

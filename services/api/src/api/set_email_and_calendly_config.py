"""One-off, idempotent script: set the tenant's outbound-email (SMTP) config
and/or its Calendly reschedule-link config, once real credentials exist.

Usage::

    python -m api.set_email_and_calendly_config [--tenant-slug SLUG]

Connects via ``DATABASE_URL_DIRECT`` (fallback ``DATABASE_URL``), same tenant
resolution as ``api.seed_sales_call_scheduling`` (reused directly, not
duplicated): ``--tenant-slug`` > ``SEED_TENANT_SLUG`` env var > the single
tenant row if exactly one exists > refuse to guess.

Reads credentials from environment variables ONLY (never CLI args, never
logged/printed) -- see the two variable groups below. Each group is
independently optional: set only the SMTP vars, only the Calendly vars, or
both, and re-run any time to update. Skips a group entirely (prints why) if
its required variables aren't set.

SMTP (outbound booking-confirmation email) -- ``PUT /admin/notifications/config``
equivalent, channel="email":
    SMTP_HOST            required
    SMTP_PORT            required (int)
    SMTP_USERNAME        required
    SMTP_PASSWORD        required -- encrypted at rest, never printed
    SMTP_FROM_ADDRESS    required
    SMTP_FROM_NAME       optional
    SMTP_USE_TLS         optional, "true"/"false", default "true"

Calendly (reschedule link embedded in booking-confirmation emails) --
``PUT /admin/schedule/calendar`` equivalent:
    CALENDLY_SCHEDULING_URL   required -- the actual booking-page link,
                              e.g. https://calendly.com/your-team/30min
    CALENDLY_ACCOUNT_ID       optional -- stored as calendar_id, informational
    CALENDLY_API_TOKEN        optional -- stored encrypted; NOT required just
                              to embed the link in an email (no Calendly API
                              calls happen for that -- see module note below)

IMPORTANT -- ``enabled`` is ALWAYS written as ``False`` for the Calendly
config here, and is NOT configurable via env var. ``enabled=True`` on this
same row means something else entirely: it tells
``GET /public/schedule/availability-summary`` to hand the visitor off to the
Calendly page INSTEAD OF the native calendar widget (see
``api.scheduling.routes``). This script only ever wants the link stored for
the confirmation email, never a flow change -- so it hardcodes ``False`` to
make that landmine impossible to trip via this script. If a native-flow
tenant's row is ever found with ``enabled=True``, this script leaves it
alone rather than silently changing flow behavior -- rerun
``api.seed_sales_call_scheduling`` (or fix it deliberately) for that.

Prints a summary of what changed. Never prints SMTP_PASSWORD, CALENDLY_API_TOKEN,
or any other secret value.
"""
from __future__ import annotations

import argparse
import asyncio
import os

from common.auth import AuthClaims, Role
from common.db import Database

from api.notifications.config_repository import upsert_notification_config
from api.scheduling.calendar_config_repository import get_calendar_config, upsert_calendar_config
from api.seed_sales_call_scheduling import (
    _resolve_tenant,  # noqa: PLC2701 -- shared tenant resolution
)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


async def _set_smtp(db: Database, claims: AuthClaims) -> None:
    host = os.environ.get("SMTP_HOST", "").strip()
    port_raw = os.environ.get("SMTP_PORT", "").strip()
    username = os.environ.get("SMTP_USERNAME", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "")
    from_address = os.environ.get("SMTP_FROM_ADDRESS", "").strip()

    if not (host and port_raw and username and password and from_address):
        print(
            "  notifications (email/SMTP): SMTP_HOST/SMTP_PORT/SMTP_USERNAME/"
            "SMTP_PASSWORD/SMTP_FROM_ADDRESS not all set -- skipped."
        )
        return

    try:
        port = int(port_raw)
    except ValueError as exc:
        raise SystemExit(f"SMTP_PORT={port_raw!r} is not a valid integer.") from exc

    await upsert_notification_config(
        db,
        claims,
        channel="email",
        provider="smtp",
        from_address=from_address,
        from_name=os.environ.get("SMTP_FROM_NAME", "").strip() or None,
        smtp_host=host,
        smtp_port=port,
        smtp_use_tls=_env_bool("SMTP_USE_TLS", True),
        smtp_username=username,
        twilio_account_sid=None,
        twilio_from=None,
        credentials=password,
        enabled=True,
    )
    print(f"  notifications (email/SMTP): configured host={host} port={port} enabled=true")


async def _set_calendly(db: Database, claims: AuthClaims) -> None:
    scheduling_url = os.environ.get("CALENDLY_SCHEDULING_URL", "").strip()
    if not scheduling_url:
        print(
            "  tenant_calendar_configs (Calendly link): "
            "CALENDLY_SCHEDULING_URL not set -- skipped."
        )
        return

    account_id = os.environ.get("CALENDLY_ACCOUNT_ID", "").strip() or None
    api_token = os.environ.get("CALENDLY_API_TOKEN", "")

    current = await get_calendar_config(db, claims)
    if current is not None and current.provider == "calendly" and current.enabled:
        print(
            "  tenant_calendar_configs (Calendly link): existing row has "
            "enabled=true (native-flow-bypass mode) -- leaving untouched. "
            "Re-run api.seed_sales_call_scheduling if that wasn't intentional."
        )
        return

    busy = current.busy if current is not None else []
    await upsert_calendar_config(
        db,
        claims,
        provider="calendly",
        calendar_id=account_id,
        credentials=api_token,
        busy=busy,
        enabled=False,
        scheduling_url=scheduling_url,
    )
    print(
        f"  tenant_calendar_configs (Calendly link): configured scheduling_url={scheduling_url} "
        f"account_id={account_id!r} enabled=false (native calendar flow unaffected)"
    )


async def _run(tenant_slug: str | None) -> None:
    dsn = os.environ.get("DATABASE_URL_DIRECT") or os.environ.get("DATABASE_URL")
    if not dsn:
        raise SystemExit("Set DATABASE_URL_DIRECT or DATABASE_URL to run this script.")

    db = await Database.connect(dsn, min_size=1, max_size=2)
    try:
        tenant_id = await _resolve_tenant(db, tenant_slug)
        claims = AuthClaims(subject="config-script", role=Role.CLIENT_ADMIN, tenant_id=tenant_id)

        print(f"=== Setting email + Calendly-link config for tenant_id={tenant_id} ===")
        await _set_smtp(db, claims)
        await _set_calendly(db, claims)
        print("=== Done ===")
    finally:
        await db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tenant-slug",
        default=None,
        help="Tenant slug to configure. Defaults to SEED_TENANT_SLUG env var, or the "
        "single tenant row if exactly one exists.",
    )
    args = parser.parse_args()
    asyncio.run(_run(args.tenant_slug))


if __name__ == "__main__":
    main()

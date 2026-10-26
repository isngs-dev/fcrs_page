"""Agents repository -- tenant-scoped async SQL for outreach settings, the
platform kill switch, consent records, the suppression list, agent run traces
and usage costs (migration 0066).

Every tenant method takes ``AuthClaims`` first and filters by
``claims.tenant_id`` (``_reject_global`` like ``contacts/repository.py``).
``get_platform_paused`` is the one global read: the platform kill switch is
not tenant data.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime, time
from typing import Any
from uuid import uuid4

from asyncpg.exceptions import ForeignKeyViolationError
from common.auth import AuthClaims, Role
from common.db import Database
from common.errors import AuthorizationError, NotFoundError, ValidationError
from common.phone import to_e164

from api.contacts.repository import get_contact
from api.leads.repository import get_lead

CHANNELS = ("email", "call")


def _reject_global(claims: AuthClaims) -> None:
    if claims.tenant_id is None:
        raise ValidationError(
            "Agents repository is tenant-scoped; PLATFORM_ADMIN callers are not permitted.",
            code="GLOBAL_CALLER_NOT_PERMITTED",
        )


# -- Settings + kill switches -------------------------------------------------


@dataclass(frozen=True)
class AgentSettings:
    """A tenant's outreach settings. The defaults ARE the product defaults (A1 D3/D4)."""

    enabled_channels: tuple[str, ...] = ()
    email_mode: str = "draft_for_review"
    outreach_paused: bool = False
    region_profile: str = "US"
    call_window_start: time = time(9, 0)
    call_window_end: time = time(20, 0)
    max_touches_per_contact: int = 3
    max_call_attempts: int = 2
    min_hours_between_touches: int = 48
    first_inbound_email_delay_minutes: int = 5
    monthly_cost_cap_micros: int | None = None
    physical_address: str | None = None
    inbound_auto_enabled: bool = False  # A4: widget leads with channel consent get a plan
    email_window_start: time = time(8, 0)  # A12 F4: contact-local e-mail working hours
    email_window_end: time = time(18, 0)


_SETTINGS_COLUMNS = [f.name for f in fields(AgentSettings)]


async def get_agent_settings(db: Database, claims: AuthClaims) -> AgentSettings:
    """The tenant's settings, or the defaults when none were saved (not persisted)."""
    _reject_global(claims)
    row = await db.fetchrow(
        "SELECT enabled_channels, email_mode, outreach_paused, region_profile, "
        "call_window_start, call_window_end, max_touches_per_contact, max_call_attempts, "
        "min_hours_between_touches, first_inbound_email_delay_minutes, "
        "monthly_cost_cap_micros, physical_address, inbound_auto_enabled, "
        "email_window_start, email_window_end "
        "FROM agent_settings WHERE tenant_id = $1",
        claims.tenant_id,
    )
    if row is None:
        return AgentSettings()
    values = dict(row)
    values["enabled_channels"] = tuple(values["enabled_channels"])
    return AgentSettings(**values)


async def upsert_agent_settings(
    db: Database, claims: AuthClaims, settings: AgentSettings,
) -> None:
    """Write every settings column (full-row upsert)."""
    _reject_global(claims)
    values: list[Any] = [getattr(settings, c) for c in _SETTINGS_COLUMNS]
    values[0] = list(settings.enabled_channels)
    await db.execute(
        "INSERT INTO agent_settings (tenant_id, enabled_channels, email_mode, outreach_paused, "
        "region_profile, call_window_start, call_window_end, max_touches_per_contact, "
        "max_call_attempts, min_hours_between_touches, first_inbound_email_delay_minutes, "
        "monthly_cost_cap_micros, physical_address, inbound_auto_enabled, "
        "email_window_start, email_window_end, updated_by) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17) "
        "ON CONFLICT (tenant_id) DO UPDATE SET enabled_channels = EXCLUDED.enabled_channels, "
        "email_mode = EXCLUDED.email_mode, outreach_paused = EXCLUDED.outreach_paused, "
        "region_profile = EXCLUDED.region_profile, "
        "call_window_start = EXCLUDED.call_window_start, "
        "call_window_end = EXCLUDED.call_window_end, "
        "max_touches_per_contact = EXCLUDED.max_touches_per_contact, "
        "max_call_attempts = EXCLUDED.max_call_attempts, "
        "min_hours_between_touches = EXCLUDED.min_hours_between_touches, "
        "first_inbound_email_delay_minutes = EXCLUDED.first_inbound_email_delay_minutes, "
        "monthly_cost_cap_micros = EXCLUDED.monthly_cost_cap_micros, "
        "physical_address = EXCLUDED.physical_address, "
        "inbound_auto_enabled = EXCLUDED.inbound_auto_enabled, "
        "email_window_start = EXCLUDED.email_window_start, "
        "email_window_end = EXCLUDED.email_window_end, "
        "updated_by = EXCLUDED.updated_by, updated_at = now()",
        claims.tenant_id,
        *values,
        claims.subject,
    )


async def set_tenant_paused(db: Database, claims: AuthClaims, *, paused: bool) -> None:
    """The tenant kill switch. Creates the settings row (with defaults) if missing."""
    _reject_global(claims)
    await db.execute(
        "INSERT INTO agent_settings (tenant_id, outreach_paused, updated_by) VALUES ($1, $2, $3) "
        "ON CONFLICT (tenant_id) DO UPDATE SET outreach_paused = EXCLUDED.outreach_paused, "
        "updated_by = EXCLUDED.updated_by, updated_at = now()",
        claims.tenant_id,
        paused,
        claims.subject,
    )


async def get_platform_paused(db: Database) -> bool:
    """The platform kill switch, read fresh (no cache). A missing row is an error, not 'off'."""
    value = await db.fetchval("SELECT value FROM platform_flags WHERE key = 'outreach_paused'")
    if value is None:
        raise RuntimeError("platform_flags.outreach_paused is missing (migration 0066 seeds it).")
    return bool(value)


async def set_platform_paused(db: Database, claims: AuthClaims, *, paused: bool) -> None:
    """Flip the platform kill switch. PLATFORM_ADMIN only."""
    if claims.role is not Role.PLATFORM_ADMIN:
        raise AuthorizationError("Platform admin only.", code="ROLE_NOT_PERMITTED")
    await db.execute(
        "UPDATE platform_flags SET value = $1, updated_by = $2, updated_at = now() "
        "WHERE key = 'outreach_paused'",
        paused,
        claims.subject,
    )


# -- Contact access ------------------------------------------------------------


async def contact_visible(db: Database, claims: AuthClaims, contact_id: str) -> bool:
    """Whether the caller may act on this contact: any contact in the tenant for
    CLIENT_ADMIN; for CLIENT_AGENT only contacts they own or whose lead is
    assigned to them."""
    contact = await get_contact(db, claims, contact_id)
    if contact is None:
        return False
    if claims.role is not Role.CLIENT_AGENT:
        return True
    if contact.owner_agent_id == claims.subject:
        return True
    if contact.lead_id is None:
        return False
    lead = await get_lead(db, claims, contact.lead_id)
    return lead is not None and lead.assigned_agent_id == claims.subject


# -- Consents ------------------------------------------------------------------


@dataclass(frozen=True)
class Consent:
    consent_id: str
    contact_id: str
    channel: str
    status: str
    basis: str
    source: str
    wording: str | None
    wording_version: str | None
    evidence: dict[str, Any]
    recorded_by: str | None
    recorded_at: datetime


_CONSENT_SELECT = (
    "SELECT consent_id, contact_id, channel, status, basis, source, wording, wording_version, "
    "evidence, recorded_by, recorded_at FROM contact_consents "
)


async def add_consent(
    db: Database,
    claims: AuthClaims,
    contact_id: str,
    *,
    channel: str,
    status: str,
    basis: str,
    source: str,
    wording: str | None = None,
    wording_version: str | None = None,
    evidence: dict[str, Any] | None = None,
) -> str:
    """Append a consent record (history is never edited). ``recorded_by`` = caller.

    ``recorded_at`` uses ``clock_timestamp()`` so two records written in one
    transaction still order correctly for "latest wins".
    """
    _reject_global(claims)
    consent_id = uuid4().hex
    try:
        await db.execute(
            "INSERT INTO contact_consents (tenant_id, consent_id, contact_id, channel, status, "
            "basis, source, wording, wording_version, evidence, recorded_by, recorded_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, clock_timestamp())",
            claims.tenant_id, consent_id, contact_id, channel, status, basis, source,
            wording, wording_version, evidence or {}, claims.subject,
        )
    except ForeignKeyViolationError as exc:
        raise NotFoundError("Contact not found.", code="CONTACT_NOT_FOUND") from exc
    return consent_id


async def list_consents(db: Database, claims: AuthClaims, contact_id: str) -> list[Consent]:
    _reject_global(claims)
    rows = await db.fetch(
        _CONSENT_SELECT + "WHERE tenant_id = $1 AND contact_id = $2 ORDER BY recorded_at DESC",
        claims.tenant_id,
        contact_id,
    )
    return [Consent(**dict(r)) for r in rows]


async def latest_consent(
    db: Database, claims: AuthClaims, contact_id: str, channel: str,
) -> Consent | None:
    """The newest record for this channel -- a revocation beats an older grant."""
    _reject_global(claims)
    row = await db.fetchrow(
        _CONSENT_SELECT + "WHERE tenant_id = $1 AND contact_id = $2 AND channel = $3 "
        "ORDER BY recorded_at DESC LIMIT 1",
        claims.tenant_id,
        contact_id,
        channel,
    )
    return Consent(**dict(row)) if row is not None else None


# -- Suppression list ----------------------------------------------------------


@dataclass(frozen=True)
class Suppression:
    suppression_id: str
    channel: str
    value_normalised: str
    reason: str
    source: str
    created_by: str | None
    created_at: datetime


def normalise_suppression_value(channel: str, value: str) -> str:
    """Email -> trimmed lower-case; call/sms -> E.164; ``all`` -> whichever the value is."""
    if channel == "email" or (channel == "all" and "@" in value):
        return value.strip().lower()
    e164 = to_e164(value)
    if e164 is None:
        raise ValidationError("Not a dialable phone number.", code="INVALID_PHONE")
    return e164


async def add_suppression(
    db: Database, claims: AuthClaims, *, channel: str, value: str, reason: str, source: str,
) -> tuple[Suppression, bool]:
    """Suppress a value. Idempotent: an existing entry is returned with ``created=False``."""
    _reject_global(claims)
    normalised = normalise_suppression_value(channel, value)
    row = await db.fetchrow(
        "INSERT INTO suppression_list "
        "(tenant_id, suppression_id, channel, value_normalised, reason, source, created_by) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7) "
        "ON CONFLICT (tenant_id, channel, value_normalised) DO NOTHING "
        "RETURNING suppression_id, channel, value_normalised, reason, source, created_by, "
        "created_at",
        claims.tenant_id, uuid4().hex, channel, normalised, reason, source, claims.subject,
    )
    if row is not None:
        return Suppression(**dict(row)), True
    existing = await db.fetchrow(
        "SELECT suppression_id, channel, value_normalised, reason, source, created_by, created_at "
        "FROM suppression_list WHERE tenant_id = $1 AND channel = $2 AND value_normalised = $3",
        claims.tenant_id, channel, normalised,
    )
    if existing is None:  # deleted between the two statements
        raise NotFoundError("Suppression entry vanished; retry.", code="SUPPRESSION_RACE")
    return Suppression(**dict(existing)), False


async def list_suppressions(
    db: Database,
    claims: AuthClaims,
    *,
    channel: str | None = None,
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[Suppression]:
    _reject_global(claims)
    pattern = None if not q else "%" + q.strip().lower().replace("\\", "\\\\").replace(
        "%", "\\%").replace("_", "\\_") + "%"
    rows = await db.fetch(
        "SELECT suppression_id, channel, value_normalised, reason, source, created_by, created_at "
        "FROM suppression_list WHERE tenant_id = $1 "
        "AND ($2::text IS NULL OR channel = $2) "
        "AND ($3::text IS NULL OR value_normalised LIKE $3) "
        "ORDER BY created_at DESC LIMIT $4 OFFSET $5",
        claims.tenant_id, channel, pattern, max(1, min(limit, 200)), max(0, offset),
    )
    return [Suppression(**dict(r)) for r in rows]


async def delete_suppression(db: Database, claims: AuthClaims, suppression_id: str) -> bool:
    _reject_global(claims)
    tag = await db.execute(
        "DELETE FROM suppression_list WHERE tenant_id = $1 AND suppression_id = $2",
        claims.tenant_id,
        suppression_id,
    )
    return str(tag).endswith(" 1")


async def is_suppressed(
    db: Database, claims: AuthClaims, *, channel: str, values: list[str],
) -> bool:
    """Whether any of ``values`` (already normalised) is suppressed for ``channel`` or ``all``."""
    _reject_global(claims)
    if not values:
        return False
    found = await db.fetchval(
        "SELECT EXISTS (SELECT 1 FROM suppression_list WHERE tenant_id = $1 "
        "AND channel IN ($2, 'all') AND value_normalised = ANY($3::text[]))",
        claims.tenant_id, channel, values,
    )
    return bool(found)


# -- Traces + costs ------------------------------------------------------------


async def start_run(
    db: Database,
    claims: AuthClaims,
    *,
    agent: str,
    correlation_id: str | None = None,
    trigger_event_id: str | None = None,
) -> str:
    _reject_global(claims)
    run_id = uuid4().hex
    await db.execute(
        "INSERT INTO agent_runs (tenant_id, run_id, correlation_id, trigger_event_id, agent) "
        "VALUES ($1, $2, $3, $4, $5)",
        claims.tenant_id, run_id, correlation_id, trigger_event_id, agent,
    )
    return run_id


async def finish_run(db: Database, claims: AuthClaims, run_id: str, *, ok: bool) -> None:
    _reject_global(claims)
    await db.execute(
        "UPDATE agent_runs SET status = $3, ended_at = now() WHERE tenant_id = $1 AND run_id = $2",
        claims.tenant_id, run_id, "ok" if ok else "failed",
    )


async def record_tool_call(
    db: Database,
    claims: AuthClaims,
    run_id: str,
    *,
    tool: str,
    args_redacted: dict[str, Any],
    result_summary: dict[str, Any] | None,
    ok: bool,
    error_code: str | None = None,
    audit_event_id: str | None = None,
) -> int:
    """Append a tool-call trace row; ``seq`` is the next number within the run."""
    _reject_global(claims)
    seq = await db.fetchval(
        "INSERT INTO agent_tool_calls (tenant_id, run_id, seq, tool, args_redacted, "
        "result_summary, ok, error_code, audit_event_id) "
        "SELECT $1, $2, coalesce(max(seq), 0) + 1, $3, $4, $5, $6, $7, $8 "
        "FROM agent_tool_calls WHERE tenant_id = $1 AND run_id = $2 RETURNING seq",
        claims.tenant_id, run_id, tool, args_redacted, result_summary, ok, error_code,
        audit_event_id,
    )
    return int(seq)


async def list_tool_calls(db: Database, claims: AuthClaims, run_id: str) -> list[dict[str, Any]]:
    _reject_global(claims)
    rows = await db.fetch(
        "SELECT seq, tool, args_redacted, result_summary, ok, error_code, audit_event_id, "
        "created_at FROM agent_tool_calls WHERE tenant_id = $1 AND run_id = $2 ORDER BY seq",
        claims.tenant_id, run_id,
    )
    return [dict(r) for r in rows]


async def month_cost_micros(db: Database, claims: AuthClaims, *, now: datetime) -> int:
    """Total recorded cost for the calendar month (UTC) containing ``now``."""
    _reject_global(claims)
    total = await db.fetchval(
        "SELECT coalesce(sum(cost_micros), 0) FROM usage_costs WHERE tenant_id = $1 "
        "AND created_at >= date_trunc('month', $2::timestamptz AT TIME ZONE 'UTC') "
        "AT TIME ZONE 'UTC'",
        claims.tenant_id, now,
    )
    return int(total)


async def month_costs_by_kind(db: Database, claims: AuthClaims, *, now: datetime) -> dict[str, int]:
    """This calendar month's (UTC) recorded cost per kind (A8 overview)."""
    _reject_global(claims)
    rows = await db.fetch(
        "SELECT kind, sum(cost_micros) AS micros FROM usage_costs WHERE tenant_id = $1 "
        "AND created_at >= date_trunc('month', $2::timestamptz AT TIME ZONE 'UTC') "
        "AT TIME ZONE 'UTC' GROUP BY kind ORDER BY kind",
        claims.tenant_id, now,
    )
    return {r["kind"]: int(r["micros"]) for r in rows}


async def suppressions_for_values(db: Database, claims: AuthClaims, values: list[str]
                                  ) -> list[Suppression]:
    """Suppression rows matching any of these normalised values (A9 H9 export)."""
    _reject_global(claims)
    rows = await db.fetch(
        "SELECT suppression_id, channel, value_normalised, reason, source, created_by, created_at "
        "FROM suppression_list WHERE tenant_id = $1 AND value_normalised = ANY($2::text[]) "
        "ORDER BY created_at", claims.tenant_id, values)
    return [Suppression(**dict(r)) for r in rows]


async def do_not_contact(db: Database, claims: AuthClaims, contact_id: str, *, reason: str,
                         source: str, channel: str = "all") -> int:
    """Suppress every e-mail address / phone number we hold for the contact (A11 M5/M6).
    ``channel`` "all" blocks every channel; an admin can remove the entries."""
    from api.contacts.repository import list_identities  # noqa: PLC0415

    values: set[str] = set()
    contact = await get_contact(db, claims, contact_id)
    for kind, value in [(i.identity_type, i.identity_value)
                        for i in await list_identities(db, claims, contact_id)] + [
            ("email", contact.email if contact else None), ("phone", contact.phone if contact
                                                            else None)]:
        if not value or kind not in ("email", "phone"):
            continue
        if channel == "email" and kind != "email" or channel == "call" and kind != "phone":
            continue
        try:
            values.add(normalise_suppression_value("email" if kind == "email" else "call", value))
        except ValidationError:
            continue
    for value in values:
        await add_suppression(db, claims, channel=channel, value=value, reason=reason,
                              source=source)
    return len(values)

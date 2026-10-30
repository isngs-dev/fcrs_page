"""ComplianceGate -- the single yes/no before any outreach touch (``specs/compliance.md`` §1).

Checks run in a fixed order and the first failure decides. Every decision is
written as an ``agent_tool_calls`` row (tool ``compliance_gate``) inside the
caller's run; blocks also write an ``outreach.gate_blocked`` audit row.
Internal errors propagate -- the gate never says "ok" because a lookup failed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from datetime import time as dtime
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from common.auth import AuthClaims
from common.db import Database
from common.phone import to_e164

from api.agents import executors
from api.agents.outreach_repository import touch_stats
from api.agents.repository import (
    AgentSettings,
    Consent,
    get_agent_settings,
    get_platform_paused,
    is_suppressed,
    latest_consent,
    month_cost_micros,
    record_tool_call,
)
from api.audit.repository import record_audit
from api.contacts.repository import get_contact, get_contact_timezone, list_identities

Channel = Literal["email", "call"]
# "first_response": the first e-mail to someone who just asked us (A12 F4) -- outreach
# rules, except the e-mail working hours. "after_call": the thank-you right after a call
# they took part in -- outreach rules, except the working hours and the gap since that call.
Purpose = Literal["outreach", "reply", "first_response", "after_call"]

# Blocks that can never clear by waiting: the task is cancelled, not rescheduled.
PERMANENT_CODES = frozenset({
    "CONTACT_NOT_FOUND", "CONTACT_SUPPRESSED", "NO_CONSENT", "CONTACT_TIMEZONE_UNKNOWN",
    "MAX_TOUCHES", "MAX_ATTEMPTS", "REGION_RULE",
})
# How long a non-permanent block without a computable "next allowed" time waits.
DEFAULT_RETRY = timedelta(hours=1)


@dataclass(frozen=True)
class GateResult:
    ok: bool
    code: str | None
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def retry_at(self) -> datetime | None:
        value = self.detail.get("retry_at")
        return datetime.fromisoformat(value) if value else None


def consent_allows(channel: str, consent: Consent | None) -> bool:
    """A1 D5: the latest record decides. Calls need written consent, or express
    consent the contact gave us directly (widget / email reply / on a call)."""
    if consent is None or consent.status != "granted":
        return False
    if channel != "call":
        return True
    if consent.basis == "express_written_consent":
        return True
    return consent.basis == "express_consent" and consent.source in {
        "widget", "email_reply", "call"}


def _within(local: datetime, start: dtime, end: dtime) -> bool:
    return start <= local.time() < end


def _next_open(at: datetime, tz: ZoneInfo, start: dtime, end: dtime) -> datetime:
    local = at.astimezone(tz)
    if _within(local, start, end):
        return at
    day = local.date() if local.time() < start else local.date() + timedelta(days=1)
    return datetime.combine(day, start, tzinfo=tz).astimezone(UTC)


def in_window(local: datetime, settings: AgentSettings) -> bool:
    return _within(local, settings.call_window_start, settings.call_window_end)


def next_window_open(at: datetime, tz: ZoneInfo, settings: AgentSettings) -> datetime:
    """The first instant >= ``at`` inside the contact-local calling window (UTC)."""
    return _next_open(at, tz, settings.call_window_start, settings.call_window_end)


def next_email_window_open(at: datetime, tz: ZoneInfo, settings: AgentSettings) -> datetime:
    """Same for the e-mail working hours (A12 F4)."""
    return _next_open(at, tz, settings.email_window_start, settings.email_window_end)


def _next_month(now: datetime) -> datetime:
    first = now.astimezone(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return (first + timedelta(days=32)).replace(day=1)


def _block(code: str, retry_at: datetime | None = None, **detail: Any) -> GateResult:
    if retry_at is None and code not in PERMANENT_CODES:
        retry_at = detail.pop("now") + DEFAULT_RETRY
    detail.pop("now", None)
    if retry_at is not None:
        detail["retry_at"] = retry_at.isoformat()
    return GateResult(ok=False, code=code, detail=detail)


async def _decide(
    db: Database, claims: AuthClaims, *, contact_id: str, channel: Channel, now: datetime,
    purpose: Purpose,
) -> GateResult:
    if await get_platform_paused(db):
        return _block("OUTREACH_PAUSED_PLATFORM", now=now)
    settings = await get_agent_settings(db, claims)
    if settings.outreach_paused:
        return _block("OUTREACH_PAUSED_TENANT", now=now)
    if channel not in settings.enabled_channels:
        return _block("CHANNEL_DISABLED", now=now, channel=channel)

    contact = await get_contact(db, claims, contact_id)
    if contact is None:
        return _block("CONTACT_NOT_FOUND")

    identity_type = "email" if channel == "email" else "phone"
    values = {i.identity_value for i in await list_identities(db, claims, contact_id)
              if i.identity_type == identity_type}
    raw = contact.email if channel == "email" else contact.phone
    if raw:
        normalised = raw.strip().lower() if channel == "email" else to_e164(raw)
        if normalised:
            values.add(normalised)
    if await is_suppressed(db, claims, channel=channel, values=sorted(values)):
        return _block("CONTACT_SUPPRESSED", channel=channel)

    if purpose == "reply":
        # A3 F9: answering someone who wrote to us is not outreach -- consent,
        # window, cadence and touch limits don't apply; the rest above does.
        if settings.region_profile != "US":
            return _block("REGION_RULE", region_profile=settings.region_profile)
        return GateResult(ok=True, code=None, detail={"channel": channel, "purpose": purpose})

    consent = await latest_consent(db, claims, contact_id, channel)
    if not consent_allows(channel, consent):
        return _block("NO_CONSENT", channel=channel,
                      consent_status=consent.status if consent else None,
                      consent_basis=consent.basis if consent else None)

    tz_name = await get_contact_timezone(db, claims, contact_id)
    try:
        tz = ZoneInfo(tz_name) if tz_name else None
    except ZoneInfoNotFoundError:
        tz = None
    if channel == "call":
        if tz is None:
            return _block("CONTACT_TIMEZONE_UNKNOWN")
        local = now.astimezone(tz)
        if not in_window(local, settings):
            return _block("OUTSIDE_CALLING_WINDOW", next_window_open(now, tz, settings),
                          local_time=local.strftime("%H:%M"))
    elif tz is not None and purpose == "outreach":  # A12 F4: follow-ups in working hours
        local = now.astimezone(tz)
        if not _within(local, settings.email_window_start, settings.email_window_end):
            return _block("OUTSIDE_EMAIL_WINDOW", next_email_window_open(now, tz, settings),
                          local_time=local.strftime("%H:%M"))

    stats = await touch_stats(db, claims, contact_id, now=now)
    gap = timedelta(hours=settings.min_hours_between_touches)
    # The gap spaces OUR follow-ups; answering someone who just asked (the inbound first
    # email, the call to a number shared in the chat) or thanking them after a call isn't one.
    if (stats.last_touch_at is not None and now - stats.last_touch_at < gap
            and purpose not in ("after_call", "first_response")):
        return _block("CADENCE_LIMIT", stats.last_touch_at + gap, touches=stats.touches)
    if stats.touches >= settings.max_touches_per_contact:
        return _block("MAX_TOUCHES", touches=stats.touches)
    if channel == "call" and stats.call_attempts >= settings.max_call_attempts:
        return _block("MAX_ATTEMPTS", call_attempts=stats.call_attempts)

    capacity = executors.get_capacity(channel)
    if capacity is not None and not await capacity(db, claims):
        return _block("CALL_CONCURRENCY_LIMIT", now + timedelta(minutes=5))

    if settings.region_profile != "US":
        return _block("REGION_RULE", region_profile=settings.region_profile)

    if settings.monthly_cost_cap_micros is not None:
        spent = await month_cost_micros(db, claims, now=now)
        if spent >= settings.monthly_cost_cap_micros:
            return _block("COST_CAP_REACHED", _next_month(now))

    return GateResult(ok=True, code=None, detail={"channel": channel, "touches": stats.touches})


async def check(
    db: Database,
    claims: AuthClaims,
    *,
    contact_id: str,
    channel: Channel,
    now: datetime,
    run_id: str,
    purpose: Purpose = "outreach",
) -> GateResult:
    """Decide, then trace (and audit a block) under ``run_id``."""
    result = await _decide(db, claims, contact_id=contact_id, channel=channel, now=now,
                           purpose=purpose)
    audit_id = None
    if not result.ok:
        audit_id = await record_audit(
            db, claims, action="outreach.gate_blocked", target_type="contact",
            target_id=contact_id,
            metadata={"channel": channel, "code": result.code, "purpose": purpose},
        )
    await record_tool_call(
        db, claims, run_id, tool="compliance_gate",
        args_redacted={"contact_id": contact_id, "channel": channel, "now": now.isoformat(),
                       "purpose": purpose},
        result_summary={"ok": result.ok, "code": result.code, **result.detail},
        ok=result.ok, error_code=result.code, audit_event_id=audit_id,
    )
    return result

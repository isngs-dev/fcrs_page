"""Inbound email (A3): receive -> thread -> outbox; then classify and act.

``receive`` runs in the webhook request (store + emit only, F5). ``handle_reply``
runs from the outbox dispatcher for ``email_reply_received``. ``handle_events``
processes SendGrid's signed event webhook (F12).
"""
from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from common.auth import AuthClaims
from common.db import Database
from common.errors import AppException
from common.logging import get_logger

from api.agents import compliance, memory, planner
from api.agents.dispatch import system_claims
from api.agents.events import emit
from api.agents.models import AgentTask
from api.agents.outreach_repository import (
    cancel_channel_tasks,
    latest_plan_ref,
    stop_contact_plan,
    stop_plan_if_empty,
)
from api.agents.repository import (
    add_suppression,
    do_not_contact,
    get_agent_settings,
    start_run,
)
from api.config import get_api_settings
from api.contacts.repository import get_contact, get_contact_id_by_identity, get_contact_timezone
from api.email_agent import replies
from api.email_agent import repository as repo
from api.email_agent.composer import assemble_body
from api.email_agent.config_repository import EmailAgentConfig, get_email_agent_config
from api.email_agent.inbound import (
    InboundEmail,
    classify_by_rules,
    parse_raw,
    plus_thread_id,
    sendgrid_auth,
    strip_quoted,
)
from api.email_agent.links import unsubscribe_url
from api.email_agent.providers import OutboundEmail
from api.email_agent.service import _booking_line, provider_for_tenant, send_message
from api.leads.repository import add_activity
from api.notifications.emit import emit_event_safe
from api.scheduling.calendar_config_repository import get_calendar_config
from api.scheduling.reminder_repository import create_reminder_jobs, skip_pending_reminders
from api.scheduling.repository import (
    cancel_event,
    create_event,
    get_availability,
    get_upcoming_for_contact,
    list_booked,
)
from api.scheduling.slots import compute_slots

_log = get_logger(__name__)

SNOOZE = timedelta(days=3)


# -- Receive (webhook request) ---------------------------------------------------------


async def _match_thread(db: Database, claims: AuthClaims, email: InboundEmail,
                        config: EmailAgentConfig | None) -> str:
    ids = [i for i in [email.in_reply_to, *email.references] if i]
    thread_id = await repo.thread_for_message_ids(db, claims, ids)
    if thread_id:
        return thread_id
    plus = plus_thread_id(email.to_addrs, config.reply_domain if config else None)
    if plus and await repo.thread_exists(db, claims, plus):
        return plus
    contact_id = None
    if email.from_addr:
        contact_id = await get_contact_id_by_identity(db, claims, identity_type="email",
                                                      identity_value=email.from_addr)
    if contact_id is not None:
        existing = await repo.newest_thread_for_contact(db, claims, contact_id)
        if existing:
            return existing
        return await repo.create_thread(db, claims, contact_id=contact_id,
                                        subject=email.subject, status="open")
    return await repo.create_thread(db, claims, contact_id=None, subject=email.subject,
                                    status="unmatched")


async def receive(db: Database, tenant_id: str, raw: bytes, *, spf: str | None,
                  dkim: str | None) -> str | None:
    """Store one inbound message and emit ``email_reply_received``. ``None`` = duplicate."""
    claims = system_claims(tenant_id)
    email = parse_raw(raw)
    if await db.fetchval("SELECT EXISTS (SELECT 1 FROM email_messages WHERE tenant_id = $1 "
                         "AND rfc_message_id = $2)", tenant_id, email.rfc_message_id):
        return None
    config = await get_email_agent_config(db, claims)
    reply_text = strip_quoted(email.text)
    rule_label = classify_by_rules(email, reply_text)
    thread_id = await _match_thread(db, claims, email, config)
    async with db.acquire() as conn, conn.transaction():
        message_id = await repo.insert_inbound(
            conn, claims, thread_id=thread_id, rfc_message_id=email.rfc_message_id,
            in_reply_to=email.in_reply_to, references=email.references,
            from_addr=email.from_addr, to_addr=", ".join(email.to_addrs)[:500],
            subject=email.subject, body_text=email.text[:50000], reply_text=reply_text,
            auth=sendgrid_auth(spf, dkim, email.from_addr))
        if message_id is not None:
            await emit(conn, claims, "email_reply_received",
                       {"message_id": message_id, "rule_label": rule_label})
    if email.attachments:
        _log.info("inbound attachments ignored", extra={"event": "inbound_attachments_ignored",
                                                         "kind": str(email.attachments)})
    return message_id


# -- Handle (outbox) ---------------------------------------------------------------------


async def handle_reply(db: Database, claims: AuthClaims, payload: dict[str, Any]) -> str:
    """Classify and act on one stored reply. Idempotent per message."""
    ctx = await repo.get_inbound_context(db, claims, payload["message_id"])
    if ctx is None:
        return "MESSAGE_GONE"
    if ctx["handled_action"]:
        return "ALREADY_HANDLED"
    reply_text: str = ctx["reply_text"] or ""
    label, source = payload.get("rule_label"), "rule"
    if label is None:
        source = "llm"
        label = (await replies.classify_reply(db, claims, reply_text)
                 if reply_text.strip() else "human_needed")

    contact = await get_contact(db, claims, ctx["contact_id"]) if ctx["contact_id"] else None
    if contact is not None:
        if label == "out_of_office":
            await _snooze(db, claims, contact.contact_id)
        elif label not in _KEEPS_PLAN:  # A12 F2: preference / bounce replies keep the plan
            await stop_contact_plan(db, claims, contact.contact_id, reason="email_reply_received")

    if contact is not None and reply_text.strip() and label not in ("bounce", "out_of_office"):
        # A11 M4: their words become part of the lead's memory before we act on them.
        kind: memory.Kind = ("objection" if label in ("not_interested", "no_calls", "no_emails",
                                                      "unsubscribe") else "asked")
        await memory.remember(db, claims, contact.contact_id, channel="email", kind=kind,
                              text=reply_text)
    action = await _act(db, claims, ctx, contact, label)
    if contact is not None and action.removesuffix("_draft") in _FOLLOW_UP_AFTER:
        await _follow_up_if_quiet(db, claims, contact.contact_id)
    await repo.record_handling(db, claims, ctx["message_id"], classification=label,
                               source=source, action=action)
    return f"{label}:{action}"


# A12 F1/F2: replies that keep the current plan, and actions after which Rebecca checks
# back once if the person goes quiet (D4: one follow-up, 2 days later).
_KEEPS_PLAN = frozenset({"no_calls", "no_emails", "bounce"})
_FOLLOW_UP_AFTER = frozenset({"answered", "link_sent"})
FOLLOW_UP_DELAY = timedelta(days=2)


async def _follow_up_if_quiet(db: Database, claims: AuthClaims, contact_id: str) -> None:
    """Plan ONE follow-up e-mail after the conversation goes quiet. A newer reply stops it
    (stop-on-reply) and plans its own, so it always follows the latest reply."""
    ref = await latest_plan_ref(db, claims, contact_id)
    try:
        await planner.create_plan(
            db, claims, contact_id=contact_id, kind=ref[1] if ref else "outbound_manual",  # type: ignore[arg-type]
            goal="follow_up_after_reply", now=datetime.now(UTC),
            campaign_id=ref[0] if ref else None,
            steps=[AgentTask(channel="email", offset=FOLLOW_UP_DELAY, goal="follow_up",
                             max_attempts=3,
                             intent="They replied and we answered; they have gone quiet since. "
                                    "Briefly check our answer helped and offer a short call.")])
    except AppException:  # a plan is already active (e.g. contact_later) -- leave it
        pass


async def _act(db: Database, claims: AuthClaims, ctx: dict[str, Any], contact: Any,
               label: str) -> str:
    if label == "unsubscribe":
        await _suppress(db, claims, ctx["from_addr"], reason="unsubscribe")
        return "suppressed"
    if label == "bounce":
        if contact is not None and contact.email:
            await _suppress(db, claims, contact.email, reason="bounce")
            # A12 F3: only e-mail is unusable; a permitted call still happens.
            await cancel_channel_tasks(db, claims, contact.contact_id, "email",
                                       reason="EMAIL_BOUNCED")
            await stop_plan_if_empty(db, claims, contact.contact_id, reason="no_channel_left")
            return "suppressed"
        return await _handoff(db, claims, ctx, contact, label, "BOUNCE_UNMATCHED")
    if label == "out_of_office":
        return "snoozed"
    if contact is None:  # unmatched: a human sorts it out
        return await _handoff(db, claims, ctx, None, label, "UNMATCHED")

    config = await get_email_agent_config(db, claims)
    if config is None or not config.enabled:
        return await _handoff(db, claims, ctx, contact, label, "EMAIL_NOT_READY")
    first = (contact.name or "").split(" ")[0] or "there"

    if label == "question":
        answer = await replies.answer_question(
            db, claims, reply_text=ctx["reply_text"] or "", first_name=first,
            memory=await memory.render(db, claims, contact.contact_id))
        if answer.band == "escalate":
            return await _handoff(db, claims, ctx, contact, label, answer.reason or "ESCALATE")
        await memory.remember(db, claims, contact.contact_id, channel="email", kind="we_said",
                              text=answer.body)
        return await _reply(db, claims, ctx, contact, config, answer.body, band="answer",
                            sources=answer.sources, action="answered")
    if label == "reschedule_meeting":  # A11 M5
        current = await get_upcoming_for_contact(db, claims, lead_id=contact.lead_id,
                                                 email=contact.email)
        if current is not None:
            moved = await _try_book(db, claims, ctx, contact)
            if moved is not None:
                await cancel_event(db, claims, current.event_id)
                await skip_pending_reminders(db, claims, current.event_id)
                await memory.remember(db, claims, contact.contact_id, channel="email",
                                      kind="booked", text=f"Moved their meeting to {moved}")
                return await _reply(db, claims, ctx, contact, config,
                                    f"Hi {first},\n\nDone — your call is now {moved}. We'll "
                                    "send you a reminder before it.", band="template",
                                    sources=[], action="rescheduled", force_send=True)
        label = "book_meeting"  # no booking to move, or that time isn't free: offer times
    if label == "cancel_meeting":  # A11 M5
        current = await get_upcoming_for_contact(db, claims, lead_id=contact.lead_id,
                                                 email=contact.email)
        if current is None:
            return await _handoff(db, claims, ctx, contact, label, "NO_BOOKING_FOUND")
        await cancel_event(db, claims, current.event_id)
        await skip_pending_reminders(db, claims, current.event_id)
        await memory.remember(db, claims, contact.contact_id, channel="email", kind="booked",
                              text="Cancelled their meeting")
        return await _reply(db, claims, ctx, contact, config,
                            f"Hi {first},\n\nNo problem — your call is cancelled. If you'd like "
                            "to find another time, just reply to this email.", band="template",
                            sources=[], action="cancelled", force_send=True)
    if label == "contact_later":  # A11 M5
        when = await _contact_later(db, claims, ctx, contact)
        if when is None:
            return await _handoff(db, claims, ctx, contact, label, "CONTACT_DATE_TOO_FAR")
        return await _reply(db, claims, ctx, contact, config,
                            f"Hi {first},\n\nOf course — I'll get back to you around "
                            f"{when:%B} {when.day}. Thanks for letting us know.",
                            band="template", sources=[], action="contact_later")
    if label == "no_calls":  # A11 M5: calls blocked, e-mail stays
        action = await _reply(db, claims, ctx, contact, config,
                              f"Hi {first},\n\nUnderstood — we won't call you, we'll keep to "
                              "email.", band="template", sources=[], action="no_calls")
        await do_not_contact(db, claims, contact.contact_id, reason="dnc_request",
                             source="inbound_email", channel="call")
        await cancel_channel_tasks(db, claims, contact.contact_id, "call", reason="NO_CALLS")
        await stop_plan_if_empty(db, claims, contact.contact_id, reason="no_channel_left")
        return action
    if label == "no_emails":  # A11 M5: e-mail blocked (no reply sent), calls stay
        await do_not_contact(db, claims, contact.contact_id, reason="unsubscribe",
                             source="inbound_email", channel="email")
        await cancel_channel_tasks(db, claims, contact.contact_id, "email", reason="NO_EMAILS")
        await stop_plan_if_empty(db, claims, contact.contact_id, reason="no_channel_left")
        return "no_emails"
    if label == "book_meeting":
        booked = await _try_book(db, claims, ctx, contact)
        if booked is not None:
            await memory.remember(db, claims, contact.contact_id, channel="email",
                                  kind="booked", text=f"Booked a call for {booked}")
            return await _reply(db, claims, ctx, contact, config,
                                f"Hi {first},\n\nYou're booked for {booked}. We'll send you a "
                                "reminder before the call.", band="template", sources=[],
                                action="booked", force_send=True)
        line = await _booking_line(db, claims, config)
        if "http" not in line:  # no link to offer -> a person agrees the time
            return await _handoff(db, claims, ctx, contact, label, "NO_BOOKING_LINK")
        return await _reply(db, claims, ctx, contact, config,
                            f"Hi {first},\n\nThanks — happy to set up a call. {line}",
                            band="template", sources=[], action="link_sent")
    if label == "interested":
        line = await _booking_line(db, claims, config)
        return await _reply(db, claims, ctx, contact, config,
                            f"Hi {first},\n\nThanks for getting back to us! {line}",
                            band="template", sources=[], action="link_sent")
    if label == "not_interested":
        action = await _reply(db, claims, ctx, contact, config,
                              f"Hi {first},\n\nThanks for letting us know — we won't follow up "
                              "again. If anything changes, just reply to this email.",
                              band="template", sources=[], action="closed")
        # A11 M6: a lasting block on every channel (after the courtesy reply went out).
        await do_not_contact(db, claims, contact.contact_id, reason="not_interested",
                             source="inbound_email")
        return action
    return await _handoff(db, claims, ctx, contact, label, "HUMAN_NEEDED")


_LATER_DEFAULT = timedelta(days=14)
_LATER_MAX = timedelta(days=180)


async def _contact_later(db: Database, claims: AuthClaims, ctx: dict[str, Any],
                         contact: Any) -> date | None:
    """Restart the follow-up from the date they asked for (14 days if none). ``None`` when
    the date is more than 180 days away -- a person decides then (A11 M5)."""
    zone_name = (await get_contact_timezone(db, claims, contact.contact_id)
                 or "America/New_York")
    now = datetime.now(UTC)
    when = await replies.extract_contact_date(db, claims, reply_text=ctx["reply_text"] or "",
                                              timezone=zone_name, now=now)
    today = now.astimezone(ZoneInfo(zone_name)).date()
    if when is None or when <= today:
        when = today + _LATER_DEFAULT
    if when - today > _LATER_MAX:
        return None
    start = datetime.combine(when, time(10), tzinfo=ZoneInfo(zone_name)).astimezone(UTC)
    try:
        await planner.create_plan(db, claims, contact_id=contact.contact_id,
                                  kind="outbound_manual", goal="contact_later", now=start)
    except AppException:  # a plan already active (raced) -- nothing more to schedule
        pass
    await memory.remember(db, claims, contact.contact_id, channel="email", kind="asked",
                          text=f"Asked us to get back to them around {when.isoformat()}")
    return when


async def _reply(db: Database, claims: AuthClaims, ctx: dict[str, Any], contact: Any,
                 config: EmailAgentConfig, body: str, *, band: str, sources: list[Any],
                 action: str, force_send: bool = False) -> str:
    """An in-thread reply to the sender, gated with ``purpose="reply"`` (F9)."""
    run_id = await start_run(db, claims, agent="email")
    gate = await compliance.check(db, claims, contact_id=contact.contact_id, channel="email",
                                  now=datetime.now(UTC), run_id=run_id, purpose="reply")
    if not gate.ok:
        return await _handoff(db, claims, ctx, contact, "blocked", gate.code or "GATE_BLOCKED")
    settings = await get_agent_settings(db, claims)
    to_addr = ctx["from_addr"]
    subject = ctx["thread_subject"] or ctx["subject"]
    subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    full = assemble_body(
        body=body, booking=None, company=config.from_name or "our team",
        physical_address=str(settings.physical_address or ""),
        unsubscribe_url=unsubscribe_url(str(claims.tenant_id), contact.contact_id, to_addr))
    # A booking confirmation goes out even in review mode: the booking already happened.
    auto = force_send or settings.email_mode == "auto_send"
    message_id = await repo.insert_reply(
        db, claims, thread_id=ctx["thread_id"],
        rfc_message_id=f"<{uuid4().hex}@{config.from_address.rpartition('@')[2]}>",
        in_reply_to=ctx["rfc_message_id"], references=[*ctx["references"], ctx["rfc_message_id"]],
        from_addr=config.from_address, to_addr=to_addr, subject=subject, body_text=full,
        status="approved" if auto else "draft", band=band, sources=sources)
    if auto:
        await send_message(db, claims, message_id, run_id=run_id)
    return action if auto else f"{action}_draft"


async def _try_book(db: Database, claims: AuthClaims, ctx: dict[str, Any],
                    contact: Any) -> str | None:
    """Book the exact open native slot the sender asked for (F10), else ``None``."""
    auth = ctx["auth"] or {}
    if not (auth.get("spf") or auth.get("dkim")):
        return None
    calendar = await get_calendar_config(db, claims)
    if calendar is not None and calendar.enabled:
        return None  # busy times live in the calendar provider -> link/human
    availability = await get_availability(db, claims)
    if availability is None:
        return None
    now = datetime.now(UTC)
    start = await replies.extract_requested_start(db, claims, reply_text=ctx["reply_text"] or "",
                                                  timezone=availability.timezone, now=now)
    if start is None:
        return None
    zone = ZoneInfo(availability.timezone)
    day: date = start.astimezone(zone).date()
    window_start = datetime.combine(day, time(0), tzinfo=zone).astimezone(UTC)
    booked = await list_booked(db, claims, window_start=window_start,
                               window_end=window_start + timedelta(days=1))
    slots = compute_slots(availability.rules, availability.timezone, day, day, booked, now=now,
                          window_max_days=get_api_settings().schedule_slot_window_max_days)
    slot = next((s for s in slots if s.starts_at == start), None)
    if slot is None:
        return None
    try:
        event = await create_event(
            db, claims, starts_at=slot.starts_at, ends_at=slot.ends_at,
            timezone=availability.timezone, visitor_id=None, lead_id=contact.lead_id,
            email=contact.email, name=contact.name,
            consent={"granted": True, "purpose": "booking", "text": "Requested by email reply",
                     "email_message_id": ctx["message_id"], "captured_at": now.isoformat()})
    except AppException:  # SLOT_UNAVAILABLE race
        return None
    await create_reminder_jobs(db, claims, event_id=event.event_id, starts_at=event.starts_at,
                               now=now)
    local = event.starts_at.astimezone(zone)
    return (f"{local:%A %B} {local.day} at {local.hour % 12 or 12}:{local:%M %p} "
            f"({availability.timezone})")


async def _suppress(db: Database, claims: AuthClaims, email: str, *, reason: str) -> None:
    await add_suppression(db, claims, channel="email", value=email, reason=reason,
                          source="inbound_email")


async def _snooze(db: Database, claims: AuthClaims, contact_id: str) -> None:
    await db.execute(
        "UPDATE outreach_tasks t SET scheduled_at = greatest(t.scheduled_at, now() + $3::interval) "
        "FROM outreach_plans p WHERE p.tenant_id = t.tenant_id AND p.plan_id = t.plan_id "
        "AND t.tenant_id = $1 AND p.contact_id = $2 AND p.status = 'active' "
        "AND t.status = 'scheduled'",
        claims.tenant_id, contact_id, SNOOZE,
    )


async def _handoff(db: Database, claims: AuthClaims, ctx: dict[str, Any], contact: Any,
                   label: str, reason: str) -> str:
    """F11: feed event + lead note + forward to the human inbox when configured."""
    await emit_event_safe(db, claims, kind="agent_email_needs_human", category="leads",
                          target_type="email_message", target_id=ctx["message_id"],
                          payload={"classification": label, "reason": reason,
                                   "contact_id": contact.contact_id if contact else None},
                          actor_id=None)
    if contact is not None and contact.lead_id:
        await add_activity(db, claims, contact.lead_id, type="agent_email_needs_human",
                           payload={"classification": label, "reason": reason,
                                    "email_message_id": ctx["message_id"]},
                           actor="system:agents")
    config = await get_email_agent_config(db, claims)
    if config is not None and config.enabled and config.reply_to:
        try:
            provider = await provider_for_tenant(db, claims, config)
            await provider.send(OutboundEmail(
                from_address=config.from_address, from_name=config.from_name,
                to=config.reply_to, subject=f"[Needs a human] {ctx['subject']}"[:150],
                body_text=(f"A reply needs a person ({label}, {reason}).\n\n"
                           f"From: {ctx['from_addr']}\n\n{ctx['reply_text'] or ''}"),
                message_id=f"<{uuid4().hex}@{config.from_address.rpartition('@')[2]}>",
                reply_to=ctx["from_addr"]))
        except Exception as exc:  # noqa: BLE001 -- the feed event above already records it
            _log.warning("human forward failed", extra={
                "event": "agent_email_forward_failed", "reason": type(exc).__name__})
    return "human"


# -- SendGrid event webhook (F12) -------------------------------------------------------------

_EVENT_REASON = {"spamreport": "complaint", "unsubscribe": "unsubscribe",
                 "group_unsubscribe": "unsubscribe"}
_DROPPED = {"Bounced Address": "bounce", "Spam Reporting Address": "complaint",
            "Unsubscribed Address": "unsubscribe"}
_KIND_FOR_REASON = {"bounce": "email_bounced", "complaint": "email_complained",
                    "unsubscribe": "opt_out_received"}


async def handle_events(db: Database, tenant_id: str, events: list[dict[str, Any]]) -> int:
    """Suppress + emit for each actionable event. Returns how many acted."""
    claims = system_claims(tenant_id)
    acted = 0
    for event in events:
        kind, email = event.get("event"), str(event.get("email") or "").strip().lower()
        if not email or "@" not in email:
            continue
        reason: str | None = None
        if kind == "bounce" and event.get("type", "bounce") == "bounce":
            reason = "bounce"
        elif kind in _EVENT_REASON:
            reason = _EVENT_REASON[str(kind)]
        elif kind == "dropped":
            reason = _DROPPED.get(str(event.get("reason")))
        if reason is None:
            continue
        # The webhook is account-wide: a SendGrid account that also sends other mail posts
        # those events too. Only an event tagged (custom_args) with one of THIS tenant's own
        # messages, about the address it went to, may suppress anyone.
        tag = event.get("message_id")
        sent = await repo.get_message(db, claims, tag) if isinstance(tag, str) and tag else None
        if sent is None or (sent.to_addr or "").strip().lower() != email:
            continue
        contact_id = await get_contact_id_by_identity(db, claims, identity_type="email",
                                                      identity_value=email)
        async with db.acquire() as conn, conn.transaction():
            _, created = await add_suppression(conn, claims, channel="email", value=email,
                                               reason=reason, source="sendgrid_event")
            if created and contact_id is not None:
                await emit(conn, claims, _KIND_FOR_REASON[reason],
                           {"contact_id": contact_id, "channel": "email"})
        acted += int(created)
    return acted

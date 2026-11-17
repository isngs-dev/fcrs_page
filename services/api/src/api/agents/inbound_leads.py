"""``inbound_lead_created`` handler (A4 H5/H7): widget lead with channel consent ->
contact linked to the lead -> consent records -> ``inbound_auto`` plan."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from common.auth import AuthClaims
from common.db import Database
from common.errors import ConflictError, ValidationError

from api.agents import memory, planner
from api.agents.consent_wording import consent_record
from api.agents.outreach_repository import (
    cancel_channel_tasks,
    delete_plans_for_contact,
    get_active_plan,
    stop_plan,
    stop_plan_if_empty,
    touch_stats,
)
from api.agents.repository import add_consent, do_not_contact, get_agent_settings
from api.contacts.repository import (
    absorb_contact,
    add_identity,
    create_contact,
    get_contact,
    get_contact_by_lead_id,
    get_contact_id_by_identity,
    get_contact_timezone,
    set_contact_timezone,
    update_contact,
)
from api.leads.repository import get_lead


async def _phone_owner(db: Database, claims: AuthClaims, phone: str | None) -> str | None:
    if not phone:
        return None
    try:
        return await get_contact_id_by_identity(db, claims, identity_type="phone",
                                                identity_value=phone)
    except ValidationError:  # undialable: nobody owns it
        return None


async def _adopt(db: Database, claims: AuthClaims, contact_id: str, lead: Any) -> str:
    """Owner, 2026-11-17: a number already on a contact is that person. The newest name and
    email win (the old email stays an identity, so it still finds them); the lead is theirs."""
    await update_contact(db, claims, contact_id, lead_id=lead.lead_id,
                         **({"name": lead.name} if lead.name else {}),
                         **({"email": lead.email} if lead.email else {}))
    for identity_type, value in (("email", lead.email), ("visitor_id", lead.visitor_id)):
        if value:
            try:
                await add_identity(db, claims, contact_id, identity_type=identity_type,
                                   identity_value=value)
            except ValidationError:
                pass  # already theirs
    return contact_id


async def _fold_in(db: Database, claims: AuthClaims, source: str, lead: Any,
                   target: str) -> bool:
    """The chat makes its contact at the email, before the number: when that number turns
    out to be ``target``'s, fold ``source`` into ``target`` -- only while ``source`` is this
    lead's own and nothing has gone out to it (one with history keeps it)."""
    contact = await get_contact(db, claims, source)
    stats = await touch_stats(db, claims, source, now=datetime.now(UTC))
    if contact is None or contact.lead_id != lead.lead_id or stats.touches or stats.call_attempts:
        return False
    async with db.acquire() as conn, conn.transaction():
        await conn.execute("UPDATE contact_consents SET contact_id = $3 "  # evidence kept
                           "WHERE tenant_id = $1 AND contact_id = $2",
                           claims.tenant_id, source, target)
        await memory.move(conn, claims, source, target)
        await delete_plans_for_contact(conn, claims, source)
        await absorb_contact(conn, claims, source, target)
    return True


async def _ensure_contact(db: Database, claims: AuthClaims, lead: Any) -> str:
    """The contact linked to this lead, else the one owning its email, else the one owning
    its number (plan 1.1), else a new one."""
    linked = await get_contact_by_lead_id(db, claims, lead.lead_id)
    if linked is not None:
        return linked.contact_id
    if lead.email:
        existing = await get_contact_id_by_identity(db, claims, identity_type="email",
                                                    identity_value=lead.email)
        if existing is not None:
            return existing
    owner = await _phone_owner(db, claims, lead.phone)
    if owner is not None:
        return await _adopt(db, claims, owner, lead)
    contact_id = await create_contact(
        db, claims, account_id=None, lead_id=lead.lead_id, name=lead.name, email=lead.email,
        phone=lead.phone, consent=lead.consent, owner_agent_id=lead.assigned_agent_id)
    for identity_type, value in (("email", lead.email), ("phone", lead.phone),
                                 ("visitor_id", lead.visitor_id)):
        if not value:
            continue
        try:
            await add_identity(db, claims, contact_id, identity_type=identity_type,
                               identity_value=value)
        except ValidationError:  # undialable phone, or identity owned by another contact
            continue
    return contact_id


async def handle_inbound_lead(db: Database, claims: AuthClaims, payload: dict[str, Any]) -> str:
    settings = await get_agent_settings(db, claims)
    if not settings.inbound_auto_enabled:
        return "AUTOMATION_OFF"
    channels: list[str] = payload.get("channels") or []
    if not channels and not payload.get("no_calls"):
        return "NO_CHANNEL_CONSENT"
    lead = await get_lead(db, claims, payload["lead_id"])
    if lead is None:
        return "LEAD_GONE"

    contact_id = await _ensure_contact(db, claims, lead)
    if payload.get("no_calls"):  # "just email me" in the chat: as an emailed "don't call" (A11 M5)
        await do_not_contact(db, claims, contact_id, reason="dnc_request", source="chat",
                             channel="call")
        await cancel_channel_tasks(db, claims, contact_id, "call", reason="NO_CALLS")
        await stop_plan_if_empty(db, claims, contact_id, reason="no_channel_left")
        if not channels:
            return "calls_stopped"
    if "call" in channels and lead.phone:
        owner = await _phone_owner(db, claims, lead.phone)
        if owner not in (None, contact_id) and await _fold_in(db, claims, contact_id, lead,
                                                              str(owner)):
            contact_id = await _adopt(db, claims, str(owner), lead)
        # The number they just agreed to be called on is the one to ring: the contact may
        # predate it (made at the email, or an earlier visit with another number).
        await update_contact(db, claims, contact_id, phone=lead.phone)
        try:
            await add_identity(db, claims, contact_id, identity_type="phone",
                               identity_value=lead.phone)
        except ValidationError:
            pass  # already theirs, or another contact's: contacts.phone still rings it
    for label, answer in (payload.get("answers") or {}).items():  # A11 M4: form answers
        await memory.remember(db, claims, contact_id, channel=payload.get("form") or "form",
                              kind="need", text=f"{label}: {answer}")
    timezone = payload.get("timezone")
    if timezone and await get_contact_timezone(db, claims, contact_id) is None:
        try:
            await set_contact_timezone(db, claims, contact_id, timezone)
        except ValidationError:
            pass  # invalid browser zone: calls drop with CONTACT_TIMEZONE_UNKNOWN (H9)
    if payload.get("consents"):  # A10: a form's own evidence (source form/meta)
        for given in payload["consents"]:
            await add_consent(
                db, claims, contact_id, channel=given["channel"], status="granted",
                basis=given["basis"], source=given["source"], wording=given.get("wording"),
                wording_version=given.get("wording_version"),
                evidence={**given.get("evidence", {}), "lead_id": lead.lead_id})
    else:
        for channel in channels:
            record = consent_record(channel, payload["version"])
            await add_consent(
                db, claims, contact_id, channel=channel, status="granted",
                basis=record["basis"], source="widget", wording=record["wording"],
                wording_version=record["wording_version"],
                evidence={"lead_id": lead.lead_id, "visitor_id": lead.visitor_id,
                          "captured_at": payload.get("captured_at"),
                          "form": payload.get("form")})
    replanned = False
    chat_call = "call" in channels and payload.get("form") == "chat"
    if "call" in channels:
        # Call consent arriving after the email-only plan (chat: email, then number):
        # rebuild the plan so it includes the call -- only if nothing has gone out yet,
        # except from the chat: they're on the site now, so the call wins (the gate's
        # cadence/touch limits still space any repeat email).
        active = await get_active_plan(db, claims, contact_id)
        if active is not None and (
                chat_call or not any(t.status == "completed" for t in active["tasks"])):
            await stop_plan(db, claims, active["plan_id"], reason="replanned_new_consent")
            replanned = True
    # A website form (it carries its own consent evidence) that said yes to calls: ring now.
    # A number shared in the chat: ring once the chat is over (dispatch waits for that).
    steps = (planner.call_first_steps(settings) if "call" in channels and payload.get("consents")
             else planner.call_first_steps(settings, planner.CHAT_CALL_INTENT) if chat_call
             else None)
    try:
        outcome = await planner.create_plan(db, claims, contact_id=contact_id, kind="inbound_auto",
                                            goal="follow_up", now=datetime.now(UTC), steps=steps)
    except ConflictError:
        return "PLAN_ALREADY_ACTIVE"
    if not outcome.plan_id:
        return "NO_ALLOWED_CHANNEL"
    return "replanned" if replanned else "planned"

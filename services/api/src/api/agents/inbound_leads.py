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
from api.agents.outreach_repository import get_active_plan, stop_plan
from api.agents.repository import add_consent, get_agent_settings
from api.contacts.repository import (
    add_identity,
    create_contact,
    get_contact_by_lead_id,
    get_contact_id_by_identity,
    get_contact_timezone,
    set_contact_timezone,
)
from api.leads.repository import get_lead


async def _ensure_contact(db: Database, claims: AuthClaims, lead: Any) -> str:
    """The contact linked to this lead, else the one owning its email, else a new one."""
    linked = await get_contact_by_lead_id(db, claims, lead.lead_id)
    if linked is not None:
        return linked.contact_id
    if lead.email:
        existing = await get_contact_id_by_identity(db, claims, identity_type="email",
                                                    identity_value=lead.email)
        if existing is not None:
            return existing
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
    if not channels:
        return "NO_CHANNEL_CONSENT"
    lead = await get_lead(db, claims, payload["lead_id"])
    if lead is None:
        return "LEAD_GONE"

    contact_id = await _ensure_contact(db, claims, lead)
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

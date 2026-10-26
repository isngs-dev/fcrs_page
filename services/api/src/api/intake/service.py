"""Lead intake pipeline (A10 I1-I4): one path for forms, Meta e-mails and the Meta API.

normalised fields -> A5 row rules -> lead (or the person's existing lead) ->
consent evidence -> the same post-capture steps as a widget lead. Invalid input
is rejected into the intake queue with its reasons -- never guessed or repaired.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from common.auth import AuthClaims
from common.db import Database

from api.agents import hooks as agent_hooks
from api.agents.imports import check_row
from api.contacts.repository import get_contact, get_contact_id_by_identity
from api.intake import meta
from api.intake import repository as repo
from api.intake.parsers import Normalised, infer_timezone, normalise
from api.leads.repository import add_activity, create_lead

_MAX_ANSWERS = 30


def preview(lead: Normalised) -> dict[str, Any]:
    """What the queue shows an admin (the fields we read; never the raw message)."""
    return {"values": lead.values, "answers": dict(list(lead.answers.items())[:_MAX_ANSWERS]),
            "form": lead.form, "campaign": lead.campaign, "call_consent": lead.call_consent}


async def _existing_lead(db: Database, claims: AuthClaims, email: str | None,
                         phone: str | None) -> str | None:
    """The lead already linked to this person (by email, then phone), if any (A10 I2)."""
    for identity_type, value in (("email", email), ("phone", phone)):
        if not value:
            continue
        contact_id = await get_contact_id_by_identity(db, claims, identity_type=identity_type,
                                                      identity_value=value)
        if contact_id is not None:
            contact = await get_contact(db, claims, contact_id)
            if contact is not None and contact.lead_id:
                return contact.lead_id
    return None


def _consents(lead: Normalised, *, source: str, intake_id: str, email: str | None,
              phone: str | None, config: repo.IntakeConfig, now: datetime
              ) -> list[dict[str, Any]]:
    """I3: the submission is email permission; AI calls only with a yes to the exact
    call-consent wording the client's form shows."""
    origin = "meta" if source.startswith("meta") else "form"
    evidence = {"intake_id": intake_id, "source": source, "form": lead.form,
                "campaign": lead.campaign, "submitted_at": now.isoformat()}
    given: list[dict[str, Any]] = []
    if email:
        given.append({
            "channel": "email", "basis": "express_consent", "source": origin,
            "wording": config.form_consent_wording or (
                f"Submitted the {lead.form or 'lead'} form asking to be contacted."),
            "evidence": evidence})
    if lead.call_consent and phone and config.call_consent_wording:
        given.append({
            "channel": "call", "basis": "express_written_consent", "source": origin,
            "wording": config.call_consent_wording, "evidence": {**evidence, "answer": "yes"}})
    return given


async def ingest(db: Database, claims: AuthClaims, *, source: str, intake_id: str,
                 lead: Normalised, config: repo.IntakeConfig) -> str:
    """Turn one recorded intake event into a lead. Returns the event's new status."""
    from api.leads.routes import after_capture  # noqa: PLC0415 -- leads.routes is an app module

    now = datetime.now(UTC)
    row, reasons = check_row(dict(lead.values), 1, now.date())
    if row is None:
        await repo.set_event(db, claims, intake_id, status="rejected",
                             reason=",".join(reasons), fields=preview(lead))
        return "rejected"
    timezone = row.timezone or infer_timezone(row.phone)
    consents = _consents(lead, source=source, intake_id=intake_id, email=row.email,
                         phone=row.phone, config=config, now=now)
    channels = [c["channel"] for c in consents]
    answers = {"form": lead.form, "campaign": lead.campaign, "source": source,
               "answers": dict(list(lead.answers.items())[:_MAX_ANSWERS])}

    existing = await _existing_lead(db, claims, row.email, row.phone)
    if existing is not None:  # a known person: same lead, same contact, no second plan
        await add_activity(db, claims, existing, type="intake_form_answers", payload=answers,
                           actor="system:intake")
        await agent_hooks.inbound_lead_created(
            db, claims, lead_id=existing, channels=channels, version=None, timezone=timezone,
            form=lead.form or source, consents=consents, answers=lead.answers)
        await repo.set_event(db, claims, intake_id, status="accepted", reason="EXISTING_LEAD",
                             lead_id=existing, fields=preview(lead))
        return "accepted"

    lead_id = await create_lead(
        db, claims, visitor_id=f"intake:{intake_id}", name=row.name, email=row.email,
        phone=row.phone, source=source,
        consent={"granted": True, "purpose": "lead_followup",
                 "text": consents[0]["wording"] if consents else "", "source": source,
                 "captured_at": now.isoformat()})
    await add_activity(db, claims, lead_id, type="intake_form_answers", payload=answers,
                       actor="system:intake")
    await after_capture(db, claims, lead_id, channels=channels, version=None,
                        timezone=timezone, form=lead.form or source, consents=consents,
                        answers=lead.answers)
    await repo.set_event(db, claims, intake_id, status="accepted", lead_id=lead_id,
                         fields=preview(lead))
    return "accepted"


async def receive(db: Database, claims: AuthClaims, *, source: str, external_id: str,
                  lead: Normalised) -> str:
    """Record (once per source id) and ingest. A repeat returns ``duplicate``."""
    config = await repo.get_config(db, claims)
    intake_id, created = await repo.record_event(
        db, claims, source=source, external_id=external_id, status="pending",
        fields=preview(lead))
    if not created:
        return "duplicate"
    if not config.enabled:
        await repo.set_event(db, claims, intake_id, status="rejected", reason="INTAKE_DISABLED")
        return "rejected"
    return await ingest(db, claims, source=source, intake_id=intake_id, lead=lead, config=config)


async def handle_meta_lead(db: Database, claims: AuthClaims, payload: dict[str, Any]) -> str:
    """Outbox handler (A10 I7): fetch the lead from the Graph API, then ingest it.
    Transient Graph errors propagate (the outbox retries with backoff)."""
    event = await repo.get_event(db, claims, payload["intake_id"])
    if event is None or event.status != "pending":
        return "ALREADY_HANDLED"
    config = await repo.get_config(db, claims)
    token = await repo.get_page_token(db, claims)
    if token is None or not config.enabled:
        reason = "META_NOT_CONNECTED" if token is None else "INTAKE_DISABLED"
        await repo.set_event(db, claims, event.intake_id, status="rejected", reason=reason)
        return reason
    try:
        fields = await meta.graph_client.fetch_lead(event.external_id, token)
    except meta.MetaLeadError as exc:
        await repo.set_event(db, claims, event.intake_id, status="rejected", reason=exc.code)
        return exc.code
    return await ingest(db, claims, source="meta_api", intake_id=event.intake_id,
                        lead=normalise(fields), config=config)

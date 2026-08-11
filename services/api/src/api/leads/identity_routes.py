"""Conversation-start identity capture routes -- POST /public/chat/identity (SR-14).

Sibling of ``leads/routes.py``'s ``POST /public/leads`` (D4 -- a distinct
endpoint, not a modification of the existing lead-capture form/endpoint):
visitor-authenticated (``get_visitor_claims``), gates persistence on explicit
consent under a NEW purpose (``chat_identification``, D7 -- the existing
``lead_followup`` copy is inaccurate for a visitor who only asked a question),
server-stamps the consent timestamp, and create-or-links a Lead by
``visitor_id`` (D5, reusing SR-9.1's exact pattern) -- filling in an existing
NULL-contact lead via ``update_lead_contact`` (D6) rather than creating a
duplicate when the visitor already has one (e.g. an anonymous SR-9.1
booking).

The response is leak-free: it never includes ``tenant_id``, ``visitor_id``,
or echoed PII.
"""
from __future__ import annotations

from datetime import UTC, datetime

from common.auth import AuthClaims
from common.errors import ValidationError
from common.logging import get_logger
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, field_validator

from api.gateway.dependencies import get_visitor_claims
from api.leads.assignment import assign_lead_fail_open
from api.leads.repository import (
    add_activity,
    create_lead,
    get_lead_id_by_visitor_id,
    update_lead_contact,
)
from api.notifications.emit import emit_event_safe

_log = get_logger(__name__)

router = APIRouter(prefix="/public/chat", tags=["chat"])


class IdentityConsentPayload(BaseModel):
    """Consent metadata provided by the visitor for the identity gate."""

    granted: bool
    purpose: str
    text: str


class IdentityCaptureRequest(BaseModel):
    """Body for POST /public/chat/identity (the SR-14 identity-gate form)."""

    name: str
    email: str
    consent: IdentityConsentPayload | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("name must not be blank")
        return v

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("email must not be blank")
        if "@" not in v:
            raise ValueError("email must contain @")
        return v


class IdentityCaptureResponse(BaseModel):
    """Leak-free response body for POST /public/chat/identity."""

    lead_id: str
    status: str


@router.post("/identity", status_code=201)
async def capture_identity(
    body: IdentityCaptureRequest,
    request: Request,
    claims: AuthClaims = Depends(get_visitor_claims),  # noqa: B008
) -> IdentityCaptureResponse:
    """Capture the visitor's identity via the conversation-start gate (SR-14).

    The ``consent`` object is required and must have ``granted=true`` for
    anything to be persisted. If consent is missing or ``granted`` is not
    exactly ``True``, returns 422 ``CONSENT_REQUIRED`` and nothing is
    persisted (C3/C4 -- capture is a transaction, not a projection; a failed
    consent gate must never silently succeed).

    ``tenant_id`` and ``visitor_id`` come ONLY from the visitor session
    (``claims``), never from the request body (C8).

    Create-or-link (D5, reusing SR-9.1's ``get_lead_id_by_visitor_id`` exact
    pattern): a visitor with no lead gets one created
    (``source="chat_identity"``); a visitor who already has a lead (e.g. an
    anonymous SR-9.1 booking that left ``name``/``email`` NULL) gets that
    SAME row filled in via ``update_lead_contact`` (D6) -- never a duplicate.
    Appends an ``identified_in_chat`` activity either way.

    Returns 201 ``{lead_id, status:"new"}`` on success. The response never
    includes ``tenant_id``, ``visitor_id``, or PII.
    """
    # -- Consent gate (GDPR, D7) --------------------------------------------
    if body.consent is None or body.consent.granted is not True:
        raise ValidationError(
            "Consent to store contact information is required.",
            code="CONSENT_REQUIRED",
        )

    # -- Stamp consent with server time (never client-supplied) -------------
    consent_with_timestamp = {
        "granted": body.consent.granted,
        "purpose": body.consent.purpose,
        "text": body.consent.text,
        "captured_at": datetime.now(UTC).isoformat(),
    }

    db = request.app.state.db

    # -- Create-or-link (D5/D6) ----------------------------------------------
    lead_id = await get_lead_id_by_visitor_id(db, claims, claims.subject)
    if lead_id is None:
        lead_id = await create_lead(
            db,
            claims,
            visitor_id=claims.subject,
            name=body.name,
            email=body.email,
            phone=None,
            consent=consent_with_timestamp,
            source="chat_identity",
        )
        # -- Round-robin auto-assignment (SR-20 D1/D2/D3): fail-open, AFTER
        # the durable lead write above, as a separate step. Only for the
        # genuinely-new-lead branch (M3's second call site) -- linking an
        # existing lead's contact (the `else` branch below) is not a
        # creation event and must not re-trigger/override assignment.
        await assign_lead_fail_open(db, claims, lead_id=lead_id)
        # -- Feed emit (SR-21 D2/D3): fail-open, AFTER the durable lead
        # write above. Only for the genuinely-new-lead branch -- linking an
        # existing lead's contact (the `else` branch) is not a capture.
        await emit_event_safe(
            db,
            claims,
            kind="lead_captured",
            category="leads",
            target_type="lead",
            target_id=lead_id,
            payload={"lead_id": lead_id},
            actor_id=None,
        )
    else:
        updated = await update_lead_contact(
            db,
            claims,
            lead_id,
            name=body.name,
            email=body.email,
            consent=consent_with_timestamp,
        )
        if not updated:
            # No silent fallback (C4): the lookup found a lead_id but the
            # scoped UPDATE matched no row (a race with another mutation, or
            # a data inconsistency) -- surface honestly rather than claim
            # success on a write that did not happen.
            raise ValidationError(
                "Could not save your details. Please try again.",
                code="IDENTITY_CAPTURE_FAILED",
            )

    await add_activity(
        db,
        claims,
        lead_id,
        type="identified_in_chat",
        payload=None,
        actor="visitor",
    )

    # -- Log the event (PII-safe) ---------------------------------------------
    _log.info(
        "identity captured",
        extra={"event": "identity_captured", "lead_id": lead_id, "tenant_id": claims.tenant_id},
    )

    return IdentityCaptureResponse(lead_id=lead_id, status="new")

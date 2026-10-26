"""Lead capture routes -- visitor-authenticated endpoint for form submission.

This is a TEMPORARY endpoint (prefixed ``/public/leads``) for lead capture
via the widget form. The endpoint is authenticated by the visitor session
(``get_visitor_claims``), gates persistence on explicit consent, and stores
the lead with a server-stamped consent record.

The response is leak-free: it never includes ``tenant_id``, ``visitor_id``,
or echoed PII.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from common.auth import AuthClaims
from common.errors import ValidationError
from common.logging import get_logger
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, field_validator

from api.agents import hooks as agent_hooks
from api.agents.consent_wording import ChannelConsentsIn, granted_channels
from api.crm.tasks import sync_lead
from api.gateway.dependencies import get_visitor_claims
from api.leads.assignment import assign_lead_fail_open
from api.leads.repository import create_lead
from api.leads.tasks import classify_lead_email
from api.notifications.emit import emit_event_safe

_log = get_logger(__name__)

router = APIRouter(prefix="/public/leads", tags=["leads"])


class ConsentPayload(BaseModel):
    """Consent metadata provided by the visitor."""

    granted: bool
    purpose: str
    text: str


class LeadCaptureRequest(BaseModel):
    """Body for POST /public/leads (widget lead form)."""

    name: str
    email: str
    phone: str | None = None
    source: str | None = None
    consent: ConsentPayload | None = None
    # A4: optional per-channel follow-up consent + the browser's IANA timezone.
    channel_consents: ChannelConsentsIn | None = None
    timezone: str | None = Field(default=None, max_length=64)

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


class LeadCaptureResponse(BaseModel):
    """Leak-free response body for POST /public/leads."""

    lead_id: str
    status: str


@router.post("", status_code=201)
async def capture_lead(
    body: LeadCaptureRequest,
    request: Request,
    claims: AuthClaims = Depends(get_visitor_claims),  # noqa: B008
) -> LeadCaptureResponse:
    """Capture a lead via the visitor session.

    The ``consent`` object is required and must have ``granted=true`` for the
    lead to be stored. If consent is missing or ``granted`` is not exactly
    ``True``, returns 422 ``CONSENT_REQUIRED`` and nothing is persisted.

    ``tenant_id`` and ``visitor_id`` come from the visitor session
    (``claims``), never from the request body.

    Returns 201 ``{lead_id, status:"new"}`` on success. The response never
    includes ``tenant_id``, ``visitor_id``, or PII.
    """
    # -- Consent gate (GDPR) -----------------------------------------------
    if body.consent is None or body.consent.granted is not True:
        raise ValidationError(
            "Consent to store contact information is required.",
            code="CONSENT_REQUIRED",
        )

    # A4 H2: reject bad channel consent BEFORE anything is stored.
    channels = granted_channels(body.channel_consents,
                                has_phone=bool(body.phone and body.phone.strip()))

    # -- Stamp consent with server time ------------------------------------
    consent_with_timestamp = {
        "granted": body.consent.granted,
        "purpose": body.consent.purpose,
        "text": body.consent.text,
        "captured_at": datetime.now(UTC).isoformat(),
    }

    # -- Capture the lead --------------------------------------------------
    db = request.app.state.db
    source = body.source or "widget"

    lead_id = await create_lead(
        db,
        claims,
        visitor_id=claims.subject,
        name=body.name,
        email=body.email,
        phone=body.phone,
        consent=consent_with_timestamp,
        source=source,
    )

    await after_capture(
        db, claims, lead_id, channels=channels,
        version=body.channel_consents.version if body.channel_consents else None,
        timezone=body.timezone, form="lead_form")

    return LeadCaptureResponse(lead_id=lead_id, status="new")


async def after_capture(
    db: Any, claims: AuthClaims, lead_id: str, *, channels: list[str], version: str | None,
    timezone: str | None, form: str, consents: list[dict[str, Any]] | None = None,
    answers: dict[str, str] | None = None,
) -> None:
    """Everything that follows a stored lead, for every source (widget, forms, Meta --
    A10 I1): assignment, the feed event, the agents hand-off, CRM sync, email check."""
    # -- Round-robin auto-assignment (SR-20 D1/D2/D3): fail-open, AFTER the
    # durable lead write above, as a separate step. Never gates or reverses
    # the capture -- an assignment failure (or round-robin simply being off,
    # the default) is invisible to the caller and leaves assigned_agent_id
    # NULL, exactly today's behavior (M2).
    await assign_lead_fail_open(db, claims, lead_id=lead_id)

    # -- Log the event (PII-safe) ------------------------------------------
    _log.info(
        "lead captured",
        extra={"event": "lead_captured", "lead_id": lead_id, "tenant_id": claims.tenant_id},
    )

    # -- Feed emit (SR-21 D2/D3): fail-open, AFTER the durable lead write
    # above. payload is ids only (lead_id) -- never name/email/phone. Never
    # gates or reverses the capture -- a feed write failing here is not
    # visible to the caller at all.
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

    # -- A4 H6: hand ticked follow-up consent to the agents (fail-safe) ------
    await agent_hooks.inbound_lead_created(
        db, claims, lead_id=lead_id, channels=channels, version=version,
        timezone=timezone, form=form, consents=consents, answers=answers)

    # -- Enqueue outbound CRM sync (fire-and-forget; S7.4 decision 4) -------
    # Never awaited, never allowed to fail the capture: an enqueue failure
    # (e.g. broker unavailable) is logged and swallowed -- the visitor still
    # gets their 201. crm.sync_lead re-derives everything it needs from the
    # trusted tenant_id + lead_id; nothing from the request body is passed.
    from common.logging import _correlation_id  # noqa: PLC0415, PLC2701

    correlation_id = _correlation_id.get() or ""
    try:
        sync_lead.delay(
            tenant_id=claims.tenant_id,
            lead_id=lead_id,
            correlation_id=correlation_id,
        )
    except Exception:
        _log.warning(
            "crm_enqueue_failed",
            extra={
                "event": "crm_enqueue_failed",
                "lead_id": lead_id,
                "tenant_id": claims.tenant_id,
            },
        )

    # -- Enqueue email qualification (fire-and-forget) ----------------------
    # Never awaited, never allowed to fail the capture: an enqueue failure is
    # logged and swallowed, same convention as the CRM sync enqueue above.
    # classify_lead_email re-derives everything it needs from the trusted
    # tenant_id + lead_id; nothing from the request body is passed.
    try:
        classify_lead_email.delay(
            tenant_id=claims.tenant_id,
            lead_id=lead_id,
            correlation_id=correlation_id,
        )
    except Exception:
        _log.warning(
            "lead_email_classify_enqueue_failed",
            extra={
                "event": "lead_email_classify_enqueue_failed",
                "lead_id": lead_id,
                "tenant_id": claims.tenant_id,
            },
        )

"""Calendly webhook receiver -- SECURITY-CRITICAL public write path (SR-6).

``POST /public/calendly/webhook/{tenant_id}`` has NO session/visitor auth --
Calendly's HMAC-SHA256 signature IS the auth (SR-6 decision 4). The path
``{tenant_id}`` is used ONLY to load that tenant's decrypted signing secret
and scope the resulting writes; it is NEVER trusted as authentication on its
own (a wrong/forged tenant_id in the path simply fails signature
verification against that tenant's key).

Verification (decision 4b/4c):
  1. Read the RAW request body bytes (``await request.body()``) -- the HMAC
     is computed over these exact bytes, never re-serialized JSON.
  2. Parse ``Calendly-Webhook-Signature: t=<unix_ts>,v1=<hex>``. Missing or
     malformed -> reject.
  3. Load the tenant's calendar config (claims-less
     ``get_calendar_config_by_tenant_id``). Not Calendly-configured / no
     signing secret -> reject.
  4. Replay check: ``abs(now - t) > tolerance`` -> reject.
  5. ``hmac.compare_digest(hmac_sha256(secret, f"{t}.{raw_body}"), v1)`` --
     constant-time. Mismatch -> reject.

Every rejection path is ``401 CALENDLY_SIGNATURE_INVALID`` (decision 4b/4c --
the SAME code for every failure mode, deliberately not distinguishing WHICH
check failed, so a probing attacker learns nothing) and writes NOTHING to
the database (decision 9, no silent fallback). The JSON body is parsed only
AFTER verification succeeds.

PII discipline (decision 9, LOAD-BEARING): the invitee email/name is NEVER
logged -- no ``extra`` field, no message string carries it. Log lines carry
only ``event_id``/``tenant_id``/``event_type``/closed-set fields.
"""
from __future__ import annotations

import hashlib
import hmac
import re
from datetime import UTC, datetime
from typing import Any

from common.auth import AuthClaims, Role
from common.errors import AuthenticationError
from common.logging import get_logger
from fastapi import APIRouter, Request, Response

from api.agents import hooks as agent_hooks
from api.config import get_api_settings
from api.notifications.recipients import resolve_event_recipient
from api.notifications.repository import enqueue_notification
from api.notifications.tasks import send_notification
from api.notifications.templates import booking_confirmation_message
from api.scheduling.calendar_config_repository import get_calendar_config_by_tenant_id
from api.scheduling.handoff_intent_repository import find_handoff_visitor
from api.scheduling.reminder_repository import create_reminder_jobs
from api.scheduling.repository import cancel_calendly_event, ingest_calendly_event

_log = get_logger(__name__)

router = APIRouter(prefix="/public/calendly", tags=["scheduling"])

_SIGNATURE_HEADER = "Calendly-Webhook-Signature"
_SIGNATURE_RE = re.compile(r"^t=(?P<t>\d+),v1=(?P<v1>[0-9a-fA-F]+)$")


class CalendlySignatureInvalidError(AuthenticationError):
    """A missing/malformed/mismatched/stale Calendly webhook signature (401).

    Deliberately the SAME code for every rejection mode (decision 4b/4c) --
    never leaks which specific check failed.
    """

    code = "CALENDLY_SIGNATURE_INVALID"
    default_message = "The Calendly webhook signature is missing or invalid."


def _parse_signature_header(raw: str | None) -> tuple[str, str] | None:
    """Parse ``t=<ts>,v1=<hex>`` -> ``(t, v1)``, or ``None`` if absent/malformed."""
    if not raw:
        return None
    match = _SIGNATURE_RE.match(raw.strip())
    if match is None:
        return None
    return match.group("t"), match.group("v1")


def _verify_signature(
    *, raw_body: bytes, header_value: str | None, secret: str, tolerance_seconds: int
) -> None:
    """Verify the Calendly signature or raise ``CalendlySignatureInvalidError``.

    Raw-body HMAC (decision 4b, load-bearing): signs over the EXACT bytes
    read from the request, never a re-serialized/re-encoded form.
    """
    parsed = _parse_signature_header(header_value)
    if parsed is None:
        raise CalendlySignatureInvalidError()
    t_str, v1 = parsed

    try:
        t = int(t_str)
    except ValueError as exc:
        raise CalendlySignatureInvalidError() from exc

    now = int(datetime.now(UTC).timestamp())
    if abs(now - t) > tolerance_seconds:
        raise CalendlySignatureInvalidError()

    signed_payload = f"{t_str}.".encode() + raw_body
    expected = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, v1):
        raise CalendlySignatureInvalidError()


def _webhook_claims(tenant_id: str) -> AuthClaims:
    """Synthetic tenant-scoped claims for the webhook's downstream calls.

    The webhook has no visitor/admin session -- ``tenant_id`` here is the
    ALREADY signature-verified path value (never trusted as auth on its own,
    per the module docstring). Some downstream repos this handler must reuse
    (reminders, notification enqueue) are ``AuthClaims``-scoped by
    convention; this synthetic claims object lets it call them without
    weakening `_reject_global` anywhere -- it carries no real identity, only
    the verified tenant boundary.
    """
    return AuthClaims(subject="calendly-webhook", role=Role.VISITOR, tenant_id=tenant_id)


def _extract_invitee_fields(payload: dict[str, Any]) -> tuple[str, str | None, str | None]:
    """Pull the Calendly UUID + optional email/name from an event payload."""
    body_payload = payload.get("payload", {}) if isinstance(payload.get("payload"), dict) else {}
    uri = str(body_payload.get("uri") or "")
    email_value = body_payload.get("email")
    email = str(email_value) if isinstance(email_value, str) and email_value else None
    name_value = body_payload.get("name")
    name = str(name_value) if isinstance(name_value, str) and name_value else None
    return uri, email, name


@router.post("/webhook/{tenant_id}", status_code=200)
async def calendly_webhook(tenant_id: str, request: Request) -> Response:
    """Ingest a Calendly ``invitee.created``/``invitee.canceled`` event.

    NO session dependency -- the signature is the sole auth (decision 4).
    Returns 200 only after a verified, processed (or intentionally no-op)
    event; every rejection is 401 ``CALENDLY_SIGNATURE_INVALID`` with
    NOTHING written (decision 9).
    """
    settings = get_api_settings()
    db = request.app.state.db

    raw_body = await request.body()
    header_value = request.headers.get(_SIGNATURE_HEADER)

    config = await get_calendar_config_by_tenant_id(db, tenant_id)
    if config is None or config.provider != "calendly" or not config.credentials:
        # Decision 4d: unknown tenant / not Calendly-configured / no secret.
        # Same rejection shape as a bad signature -- no distinguishing signal.
        raise CalendlySignatureInvalidError()

    _verify_signature(
        raw_body=raw_body,
        header_value=header_value,
        secret=config.credentials,
        tolerance_seconds=settings.calendly_webhook_tolerance_seconds,
    )

    # Parsed ONLY after verification succeeds.
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    event_type = str(body.get("event") or "")
    calendly_uuid, email, name = _extract_invitee_fields(body)

    if not calendly_uuid:
        # Malformed-but-signed payload -- acknowledge, write nothing.
        _log.warning(
            "calendly webhook missing invitee uri",
            extra={"event": "calendly_webhook_malformed", "tenant_id": tenant_id},
        )
        return Response(status_code=200)

    if event_type == "invitee.canceled":
        await cancel_calendly_event(db, tenant_id, calendly_uuid)
        _log.info(
            "calendly booking cancelled",
            extra={
                "event": "calendly_webhook_cancelled",
                "tenant_id": tenant_id,
                "source": "calendly",
            },
        )
        return Response(status_code=200)

    if event_type != "invitee.created":
        # Decision: unknown event type -> 200 acknowledged no-op. Never writes.
        _log.info(
            "calendly webhook unknown event type acknowledged",
            extra={"event": "calendly_webhook_unknown_event", "tenant_id": tenant_id},
        )
        return Response(status_code=200)

    scheduled = body.get("payload", {}).get("scheduled_event", {}) if isinstance(
        body.get("payload"), dict
    ) else {}
    start_raw = scheduled.get("start_time") if isinstance(scheduled, dict) else None
    end_raw = scheduled.get("end_time") if isinstance(scheduled, dict) else None
    timezone_value = body.get("payload", {}).get("timezone") if isinstance(
        body.get("payload"), dict
    ) else None

    if not start_raw or not end_raw:
        _log.warning(
            "calendly webhook missing scheduled_event window",
            extra={"event": "calendly_webhook_malformed", "tenant_id": tenant_id},
        )
        return Response(status_code=200)

    try:
        starts_at = datetime.fromisoformat(str(start_raw).replace("Z", "+00:00"))
        ends_at = datetime.fromisoformat(str(end_raw).replace("Z", "+00:00"))
    except ValueError:
        _log.warning(
            "calendly webhook unparseable scheduled_event window",
            extra={"event": "calendly_webhook_malformed", "tenant_id": tenant_id},
        )
        return Response(status_code=200)

    resolved_timezone = str(timezone_value) if timezone_value else "UTC"

    visitor_id: str | None = None
    if email is not None:
        visitor_id = await find_handoff_visitor(db, tenant_id, email, datetime.now(UTC))
        # None is an honest no-match (decision 5b) -- ingest anyway below.

    event = await ingest_calendly_event(
        db,
        tenant_id,
        calendly_uuid=calendly_uuid,
        starts_at=starts_at,
        ends_at=ends_at,
        timezone=resolved_timezone,
        email=email,
        name=name,
        visitor_id=visitor_id,
    )

    claims = _webhook_claims(tenant_id)
    # A2 E10: stop the contact's outreach plan (fail-safe, never raises).
    await agent_hooks.meeting_booked(db, claims, email=email)

    # Reminder jobs (decision 6d) -- same idempotent creation path as native
    # bookings; a re-delivered invitee.created hits create_reminder_jobs'
    # own ON CONFLICT DO NOTHING, so this never double-schedules.
    try:
        await create_reminder_jobs(
            db, claims, event_id=event.event_id, starts_at=event.starts_at, now=datetime.now(UTC)
        )
    except Exception:
        _log.warning(
            "calendly_reminder_creation_degraded",
            extra={"event": "calendly_reminder_creation_degraded", "tenant_id": tenant_id},
        )

    # Best-effort confirmation enqueue -- never fails the webhook (mirrors
    # book_slot's own best-effort confirmation block).
    try:
        recipient = await resolve_event_recipient(db, claims, event.event_id)
        if recipient is not None:
            subject, message_body = booking_confirmation_message(
                starts_at=event.starts_at, timezone=event.timezone
            )
            job_id = await enqueue_notification(
                db,
                claims,
                channel="email",
                recipient=recipient,
                subject=subject,
                body=message_body,
                dedupe_key=f"booking_confirm:{event.event_id}",
                payload={"kind": "booking_confirm", "event_id": event.event_id},
            )
            if job_id is not None:
                from common.logging import _correlation_id  # noqa: PLC0415, PLC2701

                correlation_id = _correlation_id.get() or ""
                send_notification.delay(
                    job_id=job_id, tenant_id=tenant_id, correlation_id=correlation_id
                )
    except Exception:
        _log.warning(
            "calendly_booking_confirm_enqueue_degraded",
            extra={"event": "calendly_booking_confirm_enqueue_degraded", "tenant_id": tenant_id},
        )

    _log.info(
        "calendly booking ingested",
        extra={
            "event": "calendly_webhook_ingested",
            "tenant_id": tenant_id,
            "source": "calendly",
            "event_id": event.event_id,
        },
    )
    return Response(status_code=200)

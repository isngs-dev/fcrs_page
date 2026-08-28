"""Twilio call-status webhook receiver -- SECURITY-CRITICAL public write path.

``POST /public/calls/twilio/{tenant_id}`` has NO session/visitor auth --
Twilio's ``X-Twilio-Signature`` IS the auth, mirroring
``api.scheduling.calendly_webhook``'s exact shape. The path ``{tenant_id}``
is used ONLY to look up that tenant's Twilio Auth Token (already stored,
encrypted, for the tenant's own SMS notification config -- this feature
never introduces a second Twilio credential) and scope the resulting SMS
enqueue; it is NEVER trusted as authentication on its own (a wrong/forged
tenant_id simply fails signature verification against that tenant's token).

Verification:
  1. Read the raw form body (Twilio POSTs
     ``application/x-www-form-urlencoded``, never JSON) and the exact
     callback URL Twilio was configured with.
  2. Load the tenant's call config (claims-less
     ``get_call_config_by_tenant_id``) and its Twilio Auth Token (claims-less
     read of ``tenant_notification_configs`` channel="sms"). Missing/
     unconfigured -> reject.
  3. ``X-Twilio-Signature`` = base64(HMAC-SHA1(auth_token, url +
     sorted-by-key concatenation of every form param's "key value" pair)).
     Constant-time compare. Mismatch -> reject.

Every rejection path is the SAME 401 ``TWILIO_SIGNATURE_INVALID`` (mirrors
Calendly's "never distinguish which check failed" doctrine) and writes
NOTHING. The form body is trusted only AFTER verification succeeds.

Once verified, only a genuinely missed call (``CallStatus`` in
``_MISSED_CALL_STATUSES``, feature ``enabled``, and ``To`` matching the
tenant's configured ``monitored_phone_number``) enqueues the text-back SMS,
via the exact same idempotent ``enqueue_notification`` + ``send_notification``
path every other notification in this system uses -- deduped on
``CallSid`` alone, so a Twilio retry or multiple terminal-status deliveries
for one call never double-sends. Twilio always gets a 200 for a verified,
processed-or-correctly-ignored request; a 4xx/5xx would make Twilio retry
the identical event, which is pointless once it has already been handled
(or correctly ignored) idempotently.
"""
from __future__ import annotations

import base64
import hashlib
import hmac

from common.auth import AuthClaims, Role
from common.errors import AuthenticationError
from common.logging import get_logger
from fastapi import APIRouter, Request, Response

from api.calls.repository import get_call_config_by_tenant_id
from api.notifications.config_repository import get_notification_config_by_tenant_id
from api.notifications.repository import enqueue_notification
from api.notifications.tasks import send_notification

_log = get_logger(__name__)

router = APIRouter(prefix="/public/calls", tags=["calls"])

_SIGNATURE_HEADER = "X-Twilio-Signature"

# CallStatus values meaning the call never connected to a person -- the
# caller is left with nothing, which is exactly who this feature exists to
# text back. "completed"/"in-progress"/"ringing"/"queued" are deliberately
# excluded (the call is/was live or still in progress).
_MISSED_CALL_STATUSES = frozenset({"no-answer", "busy", "failed", "canceled"})


class TwilioSignatureInvalidError(AuthenticationError):
    """A missing/malformed/mismatched Twilio webhook signature (401).

    Deliberately the SAME code for every rejection mode -- never leaks which
    specific check failed, mirroring ``CalendlySignatureInvalidError``.
    """

    code = "TWILIO_SIGNATURE_INVALID"
    default_message = "The Twilio webhook signature is missing or invalid."


def _compute_twilio_signature(url: str, params: dict[str, str], auth_token: str) -> str:
    """Twilio's own signature algorithm: base64(HMAC-SHA1(auth_token,
    url + "key1value1key2value2..." for every param sorted by key))."""
    signed = url + "".join(f"{key}{params[key]}" for key in sorted(params))
    digest = hmac.new(auth_token.encode("utf-8"), signed.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii")


def _verify_signature(
    *, url: str, params: dict[str, str], auth_token: str, header_value: str | None
) -> None:
    if not header_value:
        raise TwilioSignatureInvalidError()
    expected = _compute_twilio_signature(url, params, auth_token)
    if not hmac.compare_digest(expected, header_value):
        raise TwilioSignatureInvalidError()


def _webhook_claims(tenant_id: str) -> AuthClaims:
    """Synthetic tenant-scoped claims for the webhook's downstream calls.

    The webhook has no visitor/admin session -- ``tenant_id`` here is the
    ALREADY signature-verified path value. Mirrors the Calendly webhook's
    own ``_webhook_claims`` helper exactly.
    """
    return AuthClaims(subject="twilio-call-webhook", role=Role.VISITOR, tenant_id=tenant_id)


@router.post("/twilio/{tenant_id}", status_code=200)
async def twilio_call_status(tenant_id: str, request: Request) -> Response:
    """Ingest a Twilio Voice call-status callback.

    NO session dependency -- the signature is the sole auth. Returns 200
    for every verified request, whether it results in a text-back send or
    is correctly ignored (not a missed status / feature disabled / wrong
    number) -- only a signature failure returns non-200, and Twilio does not
    retry non-webhook-shaped requests it can't attribute to a real account.
    """
    db = request.app.state.db

    call_config = await get_call_config_by_tenant_id(db, tenant_id)
    sms_config = await get_notification_config_by_tenant_id(db, tenant_id, channel="sms")
    if (
        call_config is None
        or sms_config is None
        or sms_config.provider != "twilio"
        or not sms_config.credentials
    ):
        # Unknown tenant / feature never configured / SMS not configured as
        # Twilio -- same rejection shape as a bad signature, no
        # distinguishing signal.
        raise TwilioSignatureInvalidError()

    form = await request.form()
    params = {key: str(value) for key, value in form.items()}

    _verify_signature(
        url=str(request.url),
        params=params,
        auth_token=sms_config.credentials,
        header_value=request.headers.get(_SIGNATURE_HEADER),
    )

    call_sid = params.get("CallSid", "")
    call_status = params.get("CallStatus", "")
    from_number = params.get("From", "")
    to_number = params.get("To", "")

    if (
        not call_config.enabled
        or call_status not in _MISSED_CALL_STATUSES
        or to_number != call_config.monitored_phone_number
        or not call_sid
        or not from_number
    ):
        _log.info(
            "twilio call status ignored",
            extra={
                "event": "twilio_call_status_ignored",
                "tenant_id": tenant_id,
                "call_status": call_status,
            },
        )
        return Response(status_code=200)

    claims = _webhook_claims(tenant_id)
    job_id = await enqueue_notification(
        db,
        claims,
        channel="sms",
        recipient=from_number,
        subject="",
        body=call_config.text_back_message,
        dedupe_key=f"missed_call:{call_sid}",
        payload={"kind": "missed_call_textback", "call_sid": call_sid},
    )
    if job_id is not None:
        from common.logging import _correlation_id  # noqa: PLC0415, PLC2701

        correlation_id = _correlation_id.get() or ""
        send_notification.delay(job_id=job_id, tenant_id=tenant_id, correlation_id=correlation_id)
        _log.info(
            "missed call text-back enqueued",
            extra={"event": "missed_call_textback_enqueued", "tenant_id": tenant_id},
        )

    return Response(status_code=200)

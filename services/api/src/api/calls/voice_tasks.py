"""Celery task: calls.place_voice_call -- ask Twilio to dial a booking's confirmation call.

Same asyncio-loop-per-invocation shape as ``api.notifications.tasks``.
Idempotent: only a ``queued`` row is dialled; the row moves to ``calling``
as soon as Twilio accepts, so a redelivered task never places a second call.
Missing configuration or a Twilio 4xx (invalid number, unverified number on a
trial account, ...) is a deterministic ``failed`` with the reason recorded --
never retried, never silently skipped. Twilio 5xx/429/network errors retry
with backoff.
"""
from __future__ import annotations

import asyncio

import httpx
from common.db import Database
from common.logging import get_logger

from api.calls.voice import twilio_credentials_for
from api.calls.voice_repository import get_voice_call_by_id, set_voice_call_status
from api.tasks.celery_app import _CorrelationTask, celery_app

_log = get_logger(__name__)

TWILIO_API_BASE = "https://api.twilio.com/2010-04-01"


class TwilioTransientError(RuntimeError):
    """Twilio 5xx/429 or a network failure -- retried by Celery."""


@celery_app.task(  # type: ignore[untyped-decorator]
    bind=True,
    name="calls.place_voice_call",
    base=_CorrelationTask,
    max_retries=3,
)
def place_voice_call(
    self: _CorrelationTask,
    *,
    call_id: str,
    tenant_id: str,
    correlation_id: str | None = None,  # noqa: ARG001 -- consumed by _CorrelationTask.__call__
) -> dict[str, object]:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_run(call_id, tenant_id))
    except TwilioTransientError as exc:
        raise self.retry(exc=exc, countdown=30 * (2**self.request.retries)) from exc
    finally:
        loop.close()


async def _run(call_id: str, tenant_id: str) -> dict[str, object]:
    from api.config import get_api_settings  # noqa: PLC0415

    settings = get_api_settings()
    db = await Database.connect(settings.database_url, statement_cache_size=0)
    try:
        return await place_call(db, call_id=call_id, tenant_id=tenant_id)
    finally:
        await db.close()


async def place_call(db: Database, *, call_id: str, tenant_id: str) -> dict[str, object]:
    from api.config import get_api_settings  # noqa: PLC0415

    call = await get_voice_call_by_id(db, tenant_id, call_id)
    if call is None or call.status != "queued":
        return {"call_id": call_id, "status": "no_op"}

    async def fail(reason: str) -> dict[str, object]:
        await set_voice_call_status(db, tenant_id, call_id, status="failed", last_error=reason)
        _log.warning(
            "voice_call_failed",
            extra={"event": "voice_call_failed", "tenant_id": tenant_id, "error_code": reason[:60]},
        )
        return {"call_id": call_id, "status": "failed"}

    settings = get_api_settings()
    if not settings.public_api_base_url:
        return await fail("PUBLIC_API_BASE_URL_NOT_CONFIGURED")
    creds = await twilio_credentials_for(db, tenant_id)
    if creds is None:
        return await fail("TWILIO_NOT_CONFIGURED")

    base = f"{settings.public_api_base_url.rstrip('/')}/public/calls/voice/{tenant_id}/{call_id}"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                f"{TWILIO_API_BASE}/Accounts/{creds.account_sid}/Calls.json",
                auth=(creds.account_sid, creds.auth_token),
                data={
                    "To": call.to_number,
                    "From": creds.from_number,
                    "Url": f"{base}/start",
                    "Method": "POST",
                    "StatusCallback": f"{base}/status",
                    "StatusCallbackMethod": "POST",
                    "Timeout": "30",
                },
            )
    except httpx.HTTPError as exc:
        raise TwilioTransientError(f"Twilio request failed: {exc.__class__.__name__}") from exc

    if response.status_code == 429 or response.status_code >= 500:
        raise TwilioTransientError(f"Twilio returned {response.status_code}")
    if response.status_code >= 400:
        try:
            body = response.json()
            reason = f"TWILIO_{body.get('code', response.status_code)}: {body.get('message', '')}"
        except ValueError:
            reason = f"TWILIO_{response.status_code}"
        return await fail(reason[:300])

    sid = str(response.json().get("sid", ""))
    await set_voice_call_status(
        db, tenant_id, call_id, status="calling", twilio_call_sid=sid or None,
    )
    _log.info("voice_call_placed", extra={"event": "voice_call_placed", "tenant_id": tenant_id})
    return {"call_id": call_id, "status": "calling"}

"""Celery task: notifications.send_notification (S9.1).

Same asyncio-loop-per-invocation + ``Database.connect(..., statement_cache_size=0)``
shape as ``api.crm.tasks``/``api.scheduling.tasks`` (S5.1 pattern).

Idempotent + retryable; redelivery never double-sends (S9.1 decision 5):
  (a) read the job; missing -> no-op success (nothing to send);
  (b) ``status == 'sent'`` -> no-op, no send (redelivery/duplicate-call
      guard); ``status == 'failed'`` -> no-op (deterministic terminal, never
      resurrected);
  (c) load the tenant config + build the provider; no config / disabled /
      unknown provider / missing required SMTP fields -> deterministic ->
      ``mark_notification(status='failed', ...)``, return, DO NOT raise (no
      Celery retry, mirrors ``api.crm.tasks``'s deterministic branch);
  (d) ``provider.send(claims, Notification(...))``;
  (e) on success, the exactly-once flip: ``mark_notification(status='sent',
      delivery_ref=..., increment_attempt=True)`` -- guarded by
      ``status='pending'`` at the repository layer, so a concurrent delivery
      that already flipped the row causes this to match 0 rows (no second
      flip). A transient provider error (SMTP connect/timeout/disconnect/
      network) -- anything NOT a ``ValidationError`` -- propagates so Celery
      retries with backoff/jitter (``SEND_MAX_RETRIES``); the job stays ``pending``
      (attempt + reason recorded) and is retried under the SAME ``job_id`` (no
      new row). The LAST attempt marks it ``failed`` with that reason instead,
      so a job can never be left ``pending`` forever. A deterministic provider error
      (``ValidationError``, e.g. ``NotificationDeliveryError`` -- SMTP auth
      failure / permanent 5xx / malformed address) ->
      ``mark_notification(status='failed', last_error=..., increment_attempt=True)``,
      return, do NOT raise.

The task builds ``AuthClaims`` from the ``tenant_id`` kwarg -- trusted,
originates from the enqueuing route's own ``claims.tenant_id`` -- never from
visitor input, mirroring ``api.crm.tasks``/``api.scheduling.tasks``'s
system-scoped re-scoping pattern.

correlation_id (S5.1 rule): MUST be declared in the task signature. Celery
runs ``check_arguments`` inside ``apply_async`` at enqueue time, before the
base ``_CorrelationTask.__call__`` can consume it. Omitting it makes
``.delay(correlation_id=...)`` raise ``TypeError`` at enqueue.

PII discipline: log lines here carry only ``job_id``/``tenant_id``/status --
never recipient/subject/body. The actual PII-safe dispatch log line is
emitted by ``LogNotificationProvider.send`` itself.
"""
from __future__ import annotations

import asyncio
import random

from common.auth import AuthClaims, Role
from common.db import Database
from common.errors import ValidationError
from common.logging import get_logger

from api.notifications.config_repository import NotificationConfig, get_notification_config
from api.notifications.providers import Notification, notification_provider_for
from api.notifications.repository import get_notification_job, mark_notification
from api.tasks.celery_app import _CorrelationTask, celery_app

_log = get_logger(__name__)

# Transient send errors: retried 3 times, waiting ~30s, ~60s, ~120s (+ jitter).
SEND_MAX_RETRIES = 3
SEND_RETRY_BASE_SECONDS = 30


@celery_app.task(  # type: ignore[untyped-decorator]
    bind=True,
    name="notifications.send_notification",
    base=_CorrelationTask,
    max_retries=SEND_MAX_RETRIES,
)
def send_notification(
    self: _CorrelationTask,
    *,
    job_id: str,
    tenant_id: str,
    correlation_id: str | None = None,  # noqa: ARG001 — consumed by _CorrelationTask.__call__
) -> dict[str, object]:
    """Send (dispatch through the ``NotificationProvider``) a single enqueued job.

    Parameters
    ----------
    job_id, tenant_id:
        Trusted values from the enqueuing route's own ``claims``/enqueue
        result -- never from visitor input.
    correlation_id:
        Must be declared here (see module docstring). Consumed by
        ``_CorrelationTask.__call__`` before this body runs.
    """
    # A transient provider error (connect/timeout/disconnect) is retried with
    # exponential backoff + jitter; the LAST attempt records it as `failed`
    # instead of raising, so a job can never sit at `pending` forever.
    final_attempt = self.request.retries >= SEND_MAX_RETRIES
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_run(job_id, tenant_id, final_attempt=final_attempt))
    except ValidationError:
        raise
    except Exception as exc:
        countdown = SEND_RETRY_BASE_SECONDS * (2**self.request.retries) + random.uniform(0, 10)  # noqa: S311 -- retry jitter, not security
        raise self.retry(exc=exc, countdown=countdown) from exc
    finally:
        loop.close()


async def _run(job_id: str, tenant_id: str, *, final_attempt: bool = False) -> dict[str, object]:
    """Async inner body: open a DB connection and delegate to ``_execute``."""
    from api.config import get_api_settings  # noqa: PLC0415

    settings = get_api_settings()
    db = await Database.connect(settings.database_url, statement_cache_size=0)
    try:
        return await _execute(
            db,
            job_id=job_id,
            tenant_id=tenant_id,
            smtp_timeout=settings.notification_smtp_timeout_seconds,
            twilio_timeout=settings.notification_twilio_timeout_seconds,
            final_attempt=final_attempt,
        )
    finally:
        await db.close()


def _platform_email_config() -> NotificationConfig | None:
    """The platform-wide SMTP sender (``platform_smtp_*`` settings), or ``None``
    when it isn't configured -- see the settings' own comment for when it applies."""
    from api.config import get_api_settings  # noqa: PLC0415

    s = get_api_settings()
    if not s.platform_smtp_host or not s.platform_smtp_from_address:
        return None
    return NotificationConfig(
        provider="smtp",
        channel="email",
        from_address=s.platform_smtp_from_address,
        from_name=s.platform_smtp_from_name,
        smtp_host=s.platform_smtp_host,
        smtp_port=s.platform_smtp_port,
        smtp_use_tls=s.platform_smtp_use_tls,
        smtp_username=s.platform_smtp_username,
        twilio_account_sid=None,
        twilio_from=None,
        credentials=s.platform_smtp_password or "",
        enabled=True,
    )


async def _execute(
    db: Database,
    *,
    job_id: str,
    tenant_id: str,
    smtp_timeout: float,
    twilio_timeout: float = 10.0,
    final_attempt: bool = False,
) -> dict[str, object]:
    """Core re-read -> config-load -> provider-select -> send -> flip logic."""
    claims = AuthClaims(subject="system:notifications", role=Role.CLIENT_ADMIN, tenant_id=tenant_id)

    job = await get_notification_job(db, claims, job_id)
    if job is None or job.status in ("sent", "failed"):
        # Redelivery guard: a job already sent/failed (or unknown) is never
        # re-dispatched.
        _log.info(
            "notification_send_no_op",
            extra={
                "event": "notification_send_no_op",
                "job_id": job_id,
                "tenant_id": tenant_id,
                "status": job.status if job is not None else "missing",
            },
        )
        return {"job_id": job_id, "status": "no_op"}

    config = await get_notification_config(db, claims, job.channel)
    if config is None and job.channel == "email":
        config = _platform_email_config()
        if config is not None:
            _log.info(
                "notification_platform_default_used",
                extra={
                    "event": "notification_platform_default_used",
                    "job_id": job_id,
                    "tenant_id": tenant_id,
                },
            )
    if config is None or not config.enabled:
        await mark_notification(
            db,
            claims,
            job_id,
            status="failed",
            last_error="NOTIFICATION_NOT_CONFIGURED",
        )
        _log.warning(
            "notification_config_missing",
            extra={
                "event": "notification_config_missing",
                "job_id": job_id,
                "tenant_id": tenant_id,
                "reason": "no_config" if config is None else "disabled",
            },
        )
        return {"job_id": job_id, "status": "failed"}

    timeout = twilio_timeout if config.provider == "twilio" else smtp_timeout
    try:
        provider = notification_provider_for(config, timeout=timeout)
    except ValidationError as exc:
        # Deterministic config error (unknown provider / missing SMTP fields)
        # -- caught before any network call, does NOT count as an attempt,
        # do NOT raise (no Celery retry).
        await mark_notification(db, claims, job_id, status="failed", last_error=str(exc))
        _log.warning(
            "notification_config_error",
            extra={
                "event": "notification_config_error",
                "job_id": job_id,
                "tenant_id": tenant_id,
                "error_code": exc.code,
            },
        )
        return {"job_id": job_id, "status": "failed"}

    message = Notification(
        channel=job.channel, recipient=job.recipient, subject=job.subject, body=job.body
    )

    try:
        ref = await provider.send(claims, message)
    except ValidationError as exc:
        # Deterministic provider send error (SMTP auth failure, permanent
        # 5xx, malformed address) -- this WAS a real send attempt.
        await mark_notification(
            db, claims, job_id, status="failed", last_error=str(exc), increment_attempt=True
        )
        _log.warning(
            "notification_send_failed",
            extra={
                "event": "notification_send_failed",
                "job_id": job_id,
                "tenant_id": tenant_id,
                "error_code": exc.code,
            },
        )
        return {"job_id": job_id, "status": "failed"}

    except Exception as exc:
        # Transient (network/connect/timeout/disconnect) error -- a real
        # attempt, recorded with its reason. Not the last try: stays `pending`
        # and re-raises so the task retries (same job_id, no new row). Last
        # try: `failed`, so it surfaces instead of sitting pending forever.
        reason = f"{type(exc).__name__}: {exc}"[:300]
        await mark_notification(
            db,
            claims,
            job_id,
            status="failed" if final_attempt else "pending",
            last_error=reason,
            increment_attempt=True,
        )
        _log.warning(
            "notification_send_transient_error",
            extra={
                "event": "notification_send_transient_error",
                "job_id": job_id,
                "tenant_id": tenant_id,
                "error_code": type(exc).__name__,
            },
        )
        if final_attempt:
            return {"job_id": job_id, "status": "failed"}
        raise

    await mark_notification(
        db, claims, job_id, status="sent", delivery_ref=ref.ref, increment_attempt=True
    )
    _log.info(
        "notification_sent",
        extra={"event": "notification_sent", "job_id": job_id, "tenant_id": tenant_id},
    )
    return {"job_id": job_id, "status": "sent"}

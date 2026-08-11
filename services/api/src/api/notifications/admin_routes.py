"""Admin notification routes -- config + test-send (S9.1) PLUS the SR-21
in-console notifications feed. Config/test-send are ``CLIENT_ADMIN`` only
(UNCHANGED from S9.1 -- this sprint adds a router and new endpoints to this
module, but does not modify their behavior, M3); the feed endpoints are
``CLIENT_ADMIN`` + ``CLIENT_AGENT`` (both work leads and should see lead
events).

``PUT /admin/notifications/config`` sets the tenant's notification provider
config (mirrors ``PUT /admin/schedule/calendar``, S8.2 decision 2) -- the
SMTP password is encrypted at rest and never echoed back.

``POST /admin/notifications/test-send`` is the one enqueuer S9.1 ships: an
admin-initiated test-send to a caller-supplied address (S9.1 decisions 4/7 --
no visitor-consent gate applies here; the real enqueuers land in S9.2). It
idempotently enqueues via ``enqueue_notification`` and only ``.delay()``s the
send task for a genuinely NEW job -- a re-post with the same explicit
``dedupe_key`` returns ``deduplicated: true`` and does NOT enqueue a second
send (proves S9.1 decision 4 at the route).

SR-21 (D1/M3/M4): this module had only ONE router before this sprint (no
``tenant_scoped_router``). Both are now present, following the paired-
router + shared-``_impl`` pattern used across the codebase
(``contacts/admin_routes.py`` is the reference shape): one ``_impl`` per
feed operation, exposed on both ``router`` (``require_roles``) and
``tenant_scoped_router`` (``resolve_tenant_scope``, the PLATFORM_ADMIN
super-user path). The feed is DELIBERATELY NOT the outbound
``notification_jobs`` ledger this module already manages (D1) -- nothing
below reads or writes that table, sends email/SMS/WhatsApp, or shares any
code path with ``enqueue_notification``/``send_notification`` (D8).
"""
from __future__ import annotations

from typing import Any
from uuid import uuid4

from common.auth import AuthClaims, Role
from common.errors import NotFoundError, ValidationError
from common.logging import get_logger
from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from api.auth.dependencies import require_roles, resolve_tenant_scope
from api.notifications.config_repository import upsert_notification_config
from api.notifications.events_repository import (
    NotificationEvent,
    list_events,
    mark_all_read,
    mark_read,
)
from api.notifications.providers import validate_recipient_for_channel
from api.notifications.repository import (
    enqueue_notification,
    get_notification_job_id_by_dedupe_key,
)
from api.notifications.tasks import send_notification

_log = get_logger(__name__)

router = APIRouter(prefix="/admin/notifications", tags=["notifications"])
tenant_scoped_router = APIRouter(
    prefix="/admin/tenants/{tenant_id}/notifications", tags=["notifications"]
)

# D4: the feed's category vocabulary is closed -- exactly two live filter
# values (plus "no filter" = all). "mentions" does NOT exist anywhere in
# this API -- there is no @-mention concept in the product (M6) -- and is
# never accepted here.
_VALID_CATEGORIES = frozenset({"leads", "system"})


class NotificationConfigRequest(BaseModel):
    """Body for PUT /admin/notifications/config.

    ``channel`` defaults to ``"email"`` -- S9.1 back-compat: a body with no
    ``channel`` still upserts the email row.
    """

    provider: str
    channel: str = "email"
    from_address: str | None = None
    from_name: str | None = None
    smtp_host: str | None = None
    smtp_port: int | None = None
    smtp_use_tls: bool = True
    smtp_username: str | None = None
    twilio_account_sid: str | None = None
    twilio_from: str | None = None
    credentials: str = ""  # SMTP password OR Twilio Auth Token; encrypted at rest, never echoed
    enabled: bool = False


class NotificationConfigResponse(BaseModel):
    """Leak-free (no password/ciphertext/Auth Token) response for PUT .../config."""

    provider: str
    channel: str
    from_address: str | None
    from_name: str | None
    smtp_host: str | None
    smtp_port: int | None
    smtp_use_tls: bool
    smtp_username: str | None
    twilio_account_sid: str | None
    twilio_from: str | None
    enabled: bool


@router.put("/config")
async def put_notification_config(
    body: NotificationConfigRequest,
    request: Request,
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN)),  # noqa: B008
) -> NotificationConfigResponse:
    """Set the calling tenant's notification provider config. ``CLIENT_ADMIN`` only.

    The SMTP password is encrypted at rest (AES-256-GCM via ``SecretBox``)
    and never echoed back in the response (S9.1 decision 2).
    """
    await upsert_notification_config(
        request.app.state.db,
        claims,
        channel=body.channel,
        provider=body.provider,
        from_address=body.from_address,
        from_name=body.from_name,
        smtp_host=body.smtp_host,
        smtp_port=body.smtp_port,
        smtp_use_tls=body.smtp_use_tls,
        smtp_username=body.smtp_username,
        twilio_account_sid=body.twilio_account_sid,
        twilio_from=body.twilio_from,
        credentials=body.credentials,
        enabled=body.enabled,
    )

    _log.info(
        "notification config updated",
        extra={
            "event": "notification_config_set",
            "provider": body.provider,
            "channel": body.channel,
            "tenant_id": claims.tenant_id,
            "enabled": body.enabled,
        },
    )

    return NotificationConfigResponse(
        provider=body.provider,
        channel=body.channel,
        from_address=body.from_address,
        from_name=body.from_name,
        smtp_host=body.smtp_host,
        smtp_port=body.smtp_port,
        smtp_use_tls=body.smtp_use_tls,
        smtp_username=body.smtp_username,
        twilio_account_sid=body.twilio_account_sid,
        twilio_from=body.twilio_from,
        enabled=body.enabled,
    )


class TestSendRequest(BaseModel):
    """Body for POST /admin/notifications/test-send.

    ``channel`` defaults to ``"email"`` (S9.1 back-compat). ``subject``
    defaults to ``""`` -- SMS/WhatsApp have no subject field
    (``message.subject`` is ignored by ``TwilioNotificationProvider``).
    """

    channel: str = "email"
    recipient: str
    subject: str = ""
    body: str
    dedupe_key: str | None = None


class TestSendResponse(BaseModel):
    """Leak-free response for POST /admin/notifications/test-send."""

    job_id: str
    deduplicated: bool


@router.post("/test-send", status_code=202)
async def post_test_send(
    body: TestSendRequest,
    request: Request,
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN)),  # noqa: B008
) -> TestSendResponse:
    """Enqueue an admin-initiated test-send email. ``CLIENT_ADMIN`` only.

    ``dedupe_key`` defaults to a fresh ``f"test:{uuid4().hex}"`` (one per
    call) or a caller-supplied value to prove idempotency live. Only a
    genuinely NEW enqueue triggers ``send_notification.delay`` -- a
    duplicate ``dedupe_key`` returns ``deduplicated: true`` with the
    EXISTING job's id and does not enqueue a second send.
    """
    validate_recipient_for_channel(body.channel, body.recipient)

    db = request.app.state.db
    dedupe_key = body.dedupe_key or f"test:{uuid4().hex}"

    job_id = await enqueue_notification(
        db,
        claims,
        channel=body.channel,
        recipient=body.recipient,
        subject=body.subject,
        body=body.body,
        dedupe_key=dedupe_key,
    )

    if job_id is not None:
        from common.logging import _correlation_id  # noqa: PLC0415, PLC2701

        correlation_id = _correlation_id.get() or ""
        send_notification.delay(
            job_id=job_id,
            tenant_id=claims.tenant_id,
            correlation_id=correlation_id,
        )
        return TestSendResponse(job_id=job_id, deduplicated=False)

    # Conflict -- already enqueued under this dedupe_key. Look up the
    # existing job_id so the caller still gets a stable identifier back.
    existing = await get_notification_job_id_by_dedupe_key(db, claims, dedupe_key)
    return TestSendResponse(job_id=existing or "", deduplicated=True)


# =============================================================================
# SR-21: the in-console notifications feed. NOT the outbound `notification_jobs`
# ledger above -- D1. `CLIENT_ADMIN` + `CLIENT_AGENT` both list and mark-read
# (both work leads and should see lead events).
# =============================================================================


class NotificationEventResponse(BaseModel):
    """Leak-free (no ``tenant_id``) feed item. ``read`` is per-CALLER (D6)."""

    event_id: str
    kind: str
    category: str
    target_type: str | None
    target_id: str | None
    payload: dict[str, Any] | None
    actor_id: str | None
    created_at: str
    read: bool


class NotificationEventListResponse(BaseModel):
    """Envelope for ``GET /admin/notifications``."""

    items: list[NotificationEventResponse]
    total: int
    unread_count: int
    limit: int
    offset: int


def _to_event_response(event: NotificationEvent) -> NotificationEventResponse:
    return NotificationEventResponse(
        event_id=event.event_id,
        kind=event.kind,
        category=event.category,
        target_type=event.target_type,
        target_id=event.target_id,
        payload=event.payload,
        actor_id=event.actor_id,
        created_at=event.created_at.isoformat(),
        read=event.read,
    )


def _validate_category(category: str | None) -> None:
    """D4: reject any category outside the closed {leads, system} set --
    'mentions' is not, and never will be, a valid value (M6)."""
    if category is not None and category not in _VALID_CATEGORIES:
        raise ValidationError(
            f"Unknown notification category {category!r}. Valid values: "
            f"{sorted(_VALID_CATEGORIES)}.",
            code="INVALID_CATEGORY",
        )


async def _list_notifications(
    request: Request,
    claims: AuthClaims,
    *,
    category: str | None,
    unread_only: bool,
    limit: int,
    offset: int,
) -> NotificationEventListResponse:
    _validate_category(category)
    db = request.app.state.db

    clamped_limit = max(1, min(limit, 200))
    clamped_offset = max(0, offset)

    events, total, unread_count = await list_events(
        db,
        claims,
        user_id=claims.subject,
        category=category,
        unread_only=unread_only,
        limit=clamped_limit,
        offset=clamped_offset,
    )

    _log.info(
        "notifications listed",
        extra={
            "event": "notifications_listed",
            "tenant_id": claims.tenant_id,
            "result_count": len(events),
        },
    )

    return NotificationEventListResponse(
        items=[_to_event_response(e) for e in events],
        total=total,
        unread_count=unread_count,
        limit=clamped_limit,
        offset=clamped_offset,
    )


@router.get("")
async def list_notifications(
    request: Request,
    category: str | None = Query(default=None),
    unread_only: bool = Query(default=False),
    limit: int = Query(default=50),
    offset: int = Query(default=0),
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN, Role.CLIENT_AGENT)),  # noqa: B008
) -> NotificationEventListResponse:
    return await _list_notifications(
        request, claims, category=category, unread_only=unread_only, limit=limit, offset=offset,
    )


@tenant_scoped_router.get("")
async def list_notifications_for_tenant(
    request: Request,
    category: str | None = Query(default=None),
    unread_only: bool = Query(default=False),
    limit: int = Query(default=50),
    offset: int = Query(default=0),
    claims: AuthClaims = Depends(resolve_tenant_scope(Role.CLIENT_ADMIN, Role.CLIENT_AGENT)),  # noqa: B008
) -> NotificationEventListResponse:
    """PLATFORM_ADMIN super-user variant of ``GET /admin/notifications``."""
    return await _list_notifications(
        request, claims, category=category, unread_only=unread_only, limit=limit, offset=offset,
    )


class MarkReadRequest(BaseModel):
    """Body for PATCH /admin/notifications/{event_id}/read."""

    read: bool = True


class MarkReadResponse(BaseModel):
    """Leak-free response for PATCH /admin/notifications/{event_id}/read."""

    event_id: str
    read: bool


async def _patch_notification_read(
    event_id: str, body: MarkReadRequest, request: Request, claims: AuthClaims,
) -> MarkReadResponse:
    """Set/clear the CALLING user's read state for one event (D6). 404 if
    the event does not exist in the caller's tenant -- no existence leak
    for another tenant's event_id. Affects ONLY ``claims.subject``'s read
    row -- never any other user's, even in the same tenant."""
    db = request.app.state.db
    ok = await mark_read(db, claims, user_id=claims.subject, event_id=event_id, read=body.read)
    if not ok:
        raise NotFoundError("Notification event not found.", code="NOT_FOUND")
    return MarkReadResponse(event_id=event_id, read=body.read)


@router.patch("/{event_id}/read")
async def patch_notification_read(
    event_id: str,
    body: MarkReadRequest,
    request: Request,
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN, Role.CLIENT_AGENT)),  # noqa: B008
) -> MarkReadResponse:
    return await _patch_notification_read(event_id, body, request, claims)


@tenant_scoped_router.patch("/{event_id}/read")
async def patch_notification_read_for_tenant(
    event_id: str,
    body: MarkReadRequest,
    request: Request,
    claims: AuthClaims = Depends(resolve_tenant_scope(Role.CLIENT_ADMIN, Role.CLIENT_AGENT)),  # noqa: B008
) -> MarkReadResponse:
    """PLATFORM_ADMIN super-user variant of ``PATCH /admin/notifications/{event_id}/read``."""
    return await _patch_notification_read(event_id, body, request, claims)


class MarkAllReadRequest(BaseModel):
    """Body for PATCH /admin/notifications/read-all."""

    category: str | None = None


class MarkAllReadResponse(BaseModel):
    """Leak-free response for PATCH /admin/notifications/read-all."""

    marked: int


async def _mark_all_notifications_read(
    body: MarkAllReadRequest, request: Request, claims: AuthClaims,
) -> MarkAllReadResponse:
    _validate_category(body.category)
    db = request.app.state.db
    marked = await mark_all_read(db, claims, user_id=claims.subject, category=body.category)
    _log.info(
        "notifications marked read (bulk)",
        extra={
            "event": "notifications_marked_all_read",
            "tenant_id": claims.tenant_id,
            "result_count": marked,
        },
    )
    return MarkAllReadResponse(marked=marked)


@router.patch("/read-all")
async def patch_notifications_read_all(
    body: MarkAllReadRequest,
    request: Request,
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN, Role.CLIENT_AGENT)),  # noqa: B008
) -> MarkAllReadResponse:
    return await _mark_all_notifications_read(body, request, claims)


@tenant_scoped_router.patch("/read-all")
async def patch_notifications_read_all_for_tenant(
    body: MarkAllReadRequest,
    request: Request,
    claims: AuthClaims = Depends(resolve_tenant_scope(Role.CLIENT_ADMIN, Role.CLIENT_AGENT)),  # noqa: B008
) -> MarkAllReadResponse:
    """PLATFORM_ADMIN super-user variant of ``PATCH /admin/notifications/read-all``."""
    return await _mark_all_notifications_read(body, request, claims)

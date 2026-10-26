"""Email Agent routes (A2): sender config, the draft/review queue, and the public
one-click unsubscribe endpoint.

Admin routes mirror ``api.agents.routes`` (implicit + PLATFORM_ADMIN tenant-scoped).
The unsubscribe route is public: tenant and contact come ONLY from the signed
token, never from the request.
"""
from __future__ import annotations

import html
import json
from collections.abc import Callable
from typing import Any, Literal

from common.auth import AuthClaims, Role
from common.errors import NotFoundError, ValidationError
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, EmailStr, Field, HttpUrl

from api.agents.dispatch import system_claims
from api.agents.events import emit
from api.agents.repository import add_suppression, contact_visible
from api.audit.repository import record_audit
from api.auth.dependencies import get_platform_admin_actor, require_roles, resolve_tenant_scope
from api.email_agent import repository as repo
from api.email_agent.config_repository import get_email_agent_config, upsert_email_agent_config
from api.email_agent.inbound import verify_event_signature
from api.email_agent.inbound_service import handle_events, receive
from api.email_agent.links import public_url, sign_route, verify_route, verify_unsubscribe
from api.email_agent.service import approve, discard

router = APIRouter(prefix="/admin/outreach", tags=["email-agent"])
tenant_scoped_router = APIRouter(prefix="/admin/tenants/{tenant_id}/outreach",
                                 tags=["email-agent"])
public_router = APIRouter(prefix="/public/email", tags=["email-agent"])


class EmailConfigBody(BaseModel):
    provider: Literal["sendgrid", "smtp"]
    from_address: EmailStr
    from_name: str = Field(min_length=1, max_length=80)  # the business name (sender + footer)
    reply_to: EmailStr | None = None
    booking_url: HttpUrl | None = None
    enabled: bool = True
    api_key: str | None = Field(default=None, max_length=500)  # write-only; "" clears
    # A3: replies to reply+{thread}@reply_domain reach the inbound webhook (MX -> SendGrid).
    reply_domain: str | None = Field(default=None, max_length=253,
                                     pattern=r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")
    # SendGrid signed Event Webhook verification key (base64 DER public key).
    event_webhook_public_key: str | None = Field(default=None, max_length=500)


def _config_out(config: Any, tenant_id: str | None = None) -> dict[str, Any]:
    if config is None:
        return {"configured": False}
    out = {"configured": True, "provider": config.provider, "from_address": config.from_address,
           "from_name": config.from_name, "reply_to": config.reply_to,
           "booking_url": config.booking_url, "has_api_key": config.has_api_key,
           "enabled": config.enabled, "reply_domain": config.reply_domain,
           "has_event_key": bool(config.event_webhook_public_key)}
    if tenant_id is not None:  # the two URLs to paste into SendGrid (A3 F1/F12)
        token = sign_route(tenant_id)
        out["inbound_parse_url"] = public_url(f"/public/email/inbound/{token}")
        out["event_webhook_url"] = public_url(f"/public/email/events/{token}")
    return out


def _message_out(m: repo.EmailMessageRow, *, full: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {
        "message_id": m.message_id, "thread_id": m.thread_id, "contact_id": m.contact_id,
        "status": m.status, "band": m.band, "direction": m.direction, "from": m.from_addr,
        "to": m.to_addr, "subject": m.subject, "classification": m.classification,
        "handled_action": m.handled_action,
        "task_id": m.task_id, "last_error": m.last_error, "approved_by": m.approved_by,
        "created_at": m.created_at.isoformat(),
        "sent_at": m.sent_at.isoformat() if m.sent_at else None,
    }
    if full:
        out.update(body_text=m.body_text, sources=m.sources, rfc_message_id=m.rfc_message_id,
                   in_reply_to=m.in_reply_to)
    return out


async def _can_see(db: Any, claims: AuthClaims, m: repo.EmailMessageRow) -> bool:
    """Unmatched inbound mail (no contact) is for admins; agents see their own contacts."""
    if m.contact_id is None:
        return claims.role is not Role.CLIENT_AGENT
    return await contact_visible(db, claims, m.contact_id)


async def _visible_message(request: Request, claims: AuthClaims,
                           message_id: str) -> repo.EmailMessageRow:
    db = request.app.state.db
    message = await repo.get_message(db, claims, message_id)
    if message is None or not await _can_see(db, claims, message):
        raise NotFoundError("Email not found.", code="EMAIL_NOT_FOUND")
    return message


class DraftEdit(BaseModel):
    subject: str = Field(min_length=1, max_length=150)
    body_text: str = Field(min_length=1, max_length=20000)


def _mount(r: APIRouter, admin: Callable[..., Any], staff: Callable[..., Any]) -> None:

    @r.get("/email-config")
    async def get_config(request: Request,
                         claims: AuthClaims = Depends(staff)) -> dict[str, Any]:  # noqa: B008
        config = await get_email_agent_config(request.app.state.db, claims)
        urls = claims.role is Role.CLIENT_ADMIN and config is not None
        return _config_out(config, str(claims.tenant_id) if urls else None)

    @r.put("/email-config")
    async def put_config(body: EmailConfigBody, request: Request,
                         claims: AuthClaims = Depends(admin)) -> dict[str, Any]:  # noqa: B008
        if body.provider == "sendgrid" and body.api_key == "":
            raise ValidationError("SendGrid needs an API key.", code="EMAIL_CONFIG_INVALID")
        db = request.app.state.db
        # A8: the event key is kept when omitted (like api_key); "" clears it.
        event_key = body.event_webhook_public_key
        if event_key is None:
            current = await get_email_agent_config(db, claims)
            event_key = current.event_webhook_public_key if current else None
        await upsert_email_agent_config(
            db, claims, provider=body.provider, from_address=str(body.from_address).lower(),
            from_name=body.from_name.strip(),
            reply_to=str(body.reply_to) if body.reply_to else None,
            booking_url=str(body.booking_url) if body.booking_url else None,
            enabled=body.enabled, api_key=body.api_key,
            reply_domain=body.reply_domain.lower() if body.reply_domain else None,
            event_webhook_public_key=event_key or None)
        config = await get_email_agent_config(db, claims)
        if config is not None and config.provider == "sendgrid" and not config.has_api_key:
            raise ValidationError("SendGrid needs an API key.", code="EMAIL_CONFIG_INVALID")
        await record_audit(db, claims, action="outreach.email_config_updated",
                           target_type="tenant_email_agent_configs", target_id=claims.tenant_id,
                           metadata={"provider": body.provider,
                                     "api_key_changed": body.api_key is not None},
                           actor_context=get_platform_admin_actor(request))
        return _config_out(config, str(claims.tenant_id))

    @r.get("/emails")
    async def list_emails(
        request: Request,
        status: Literal["draft", "approved", "sending", "sent", "failed",
                        "discarded", "received"] | None = None,
        direction: Literal["outbound", "inbound"] | None = None,
        limit: int = 50,
        offset: int = 0,
        claims: AuthClaims = Depends(staff),  # noqa: B008
    ) -> list[dict[str, Any]]:
        db = request.app.state.db
        rows = await repo.list_messages(db, claims, status=status, direction=direction,
                                        limit=limit, offset=offset)
        if claims.role is Role.CLIENT_AGENT:
            rows = [m for m in rows if await _can_see(db, claims, m)]
        return [_message_out(m) for m in rows]

    @r.get("/emails/{message_id}")
    async def get_email(message_id: str, request: Request,
                        claims: AuthClaims = Depends(staff)) -> dict[str, Any]:  # noqa: B008
        return _message_out(await _visible_message(request, claims, message_id), full=True)

    @r.put("/emails/{message_id}")
    async def edit_email(message_id: str, body: DraftEdit, request: Request,
                         claims: AuthClaims = Depends(staff)) -> dict[str, Any]:  # noqa: B008
        await _visible_message(request, claims, message_id)
        db = request.app.state.db
        if not await repo.update_draft(db, claims, message_id, subject=body.subject,
                                       body_text=body.body_text):
            raise ValidationError("Only drafts can be edited.", code="EMAIL_NOT_DRAFT")
        await record_audit(db, claims, action="outreach.email_draft_edited",
                           target_type="email_message", target_id=message_id,
                           actor_context=get_platform_admin_actor(request))
        return _message_out(await _visible_message(request, claims, message_id), full=True)

    @r.post("/emails/{message_id}:approve")
    async def approve_email(message_id: str, request: Request,
                            claims: AuthClaims = Depends(staff)) -> dict[str, Any]:  # noqa: B008
        await _visible_message(request, claims, message_id)
        db = request.app.state.db
        result = await approve(db, claims, message_id)
        await record_audit(db, claims, action="outreach.email_approved",
                           target_type="email_message", target_id=message_id,
                           metadata={"result": result["status"]},
                           actor_context=get_platform_admin_actor(request))
        return result

    @r.post("/emails/{message_id}:discard")
    async def discard_email(message_id: str, request: Request,
                            claims: AuthClaims = Depends(staff)) -> dict[str, str]:  # noqa: B008
        await _visible_message(request, claims, message_id)
        db = request.app.state.db
        await discard(db, claims, message_id)
        await record_audit(db, claims, action="outreach.email_discarded",
                           target_type="email_message", target_id=message_id,
                           actor_context=get_platform_admin_actor(request))
        return {"message_id": message_id, "status": "discarded"}


_mount(router, require_roles(Role.CLIENT_ADMIN),
       require_roles(Role.CLIENT_ADMIN, Role.CLIENT_AGENT))
_platform = resolve_tenant_scope(Role.CLIENT_ADMIN)
_mount(tenant_scoped_router, _platform, _platform)


# -- Public one-click unsubscribe (RFC 8058) -------------------------------------------

_PAGE = (
    "<!doctype html><html><head><meta charset='utf-8'>"
    "<meta name='viewport' content='width=device-width,initial-scale=1'>"
    "<title>{title}</title></head><body style='font-family:Arial,Helvetica,sans-serif;"
    "max-width:480px;margin:48px auto;padding:0 16px;color:#1e293b'>{body}</body></html>"
)


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(_PAGE.format(title=html.escape(title), body=body), status_code=status)


_INVALID = ("Link not valid", "<p>This unsubscribe link isn't valid.</p>", 404)


@public_router.get("/unsubscribe/{token}")
async def unsubscribe_page(token: str) -> HTMLResponse:
    """Confirm page. GET never unsubscribes (mail scanners follow links)."""
    target = verify_unsubscribe(token)
    if target is None:
        return _page(*_INVALID)
    return _page("Unsubscribe", (
        f"<p>Stop emails to <b>{html.escape(target.email)}</b>?</p>"
        f"<form method='post'><button type='submit' style='padding:10px 18px;font-size:15px'>"
        "Unsubscribe</button></form>"))


@public_router.post("/unsubscribe/{token}")
async def unsubscribe(token: str, request: Request) -> HTMLResponse:
    """One-click (``List-Unsubscribe=One-Click``) or the confirm button. Idempotent."""
    target = verify_unsubscribe(token)
    if target is None:
        return _page(*_INVALID)
    db = request.app.state.db
    claims = system_claims(target.tenant_id)
    async with db.acquire() as conn, conn.transaction():
        _, created = await add_suppression(conn, claims, channel="email", value=target.email,
                                           reason="unsubscribe", source="unsubscribe_link")
        if created:
            await emit(conn, claims, "opt_out_received",
                       {"contact_id": target.contact_id, "channel": "email"})
    if created:
        await record_audit(db, claims, action="outreach.unsubscribed", target_type="contact",
                           target_id=target.contact_id, metadata={"channel": "email"})
    return _page("Unsubscribed", "<p>You're unsubscribed. You won't get these emails again.</p>")


# -- Inbound webhooks (A3) --------------------------------------------------------------
# Tenant comes ONLY from the signed route token. Unknown token -> 404 (SendGrid
# stops retrying); a storage error -> 5xx so SendGrid retries (idempotent).

_MAX_RAW_BYTES = 10 * 1024 * 1024
_EVENT_MAX_AGE_SECONDS = 600


@public_router.post("/inbound/{token}")
async def inbound_parse(token: str, request: Request) -> JSONResponse:
    """SendGrid Inbound Parse, "POST the raw, full MIME message" mode."""
    tenant_id = verify_route(token)
    if tenant_id is None:
        return JSONResponse({"error_code": "NOT_FOUND"}, status_code=404)
    form = await request.form(max_part_size=_MAX_RAW_BYTES)
    raw = form.get("email")
    if raw is None:
        return JSONResponse({"error_code": "RAW_MIME_REQUIRED"}, status_code=422)
    data = raw.encode() if isinstance(raw, str) else await raw.read()
    spf, dkim = form.get("SPF"), form.get("dkim")
    message_id = await receive(request.app.state.db, tenant_id, data,
                               spf=spf if isinstance(spf, str) else None,
                               dkim=dkim if isinstance(dkim, str) else None)
    return JSONResponse({"stored": message_id is not None})


@public_router.post("/events/{token}")
async def event_webhook(token: str, request: Request) -> JSONResponse:
    """SendGrid signed Event Webhook (bounces, complaints, unsubscribes)."""
    import time as _time  # noqa: PLC0415

    tenant_id = verify_route(token)
    if tenant_id is None:
        return JSONResponse({"error_code": "NOT_FOUND"}, status_code=404)
    db = request.app.state.db
    config = await get_email_agent_config(db, system_claims(tenant_id))
    signature = request.headers.get("X-Twilio-Email-Event-Webhook-Signature", "")
    timestamp = request.headers.get("X-Twilio-Email-Event-Webhook-Timestamp", "")
    body = await request.body()
    fresh = timestamp.isdigit() and abs(_time.time() - int(timestamp)) <= _EVENT_MAX_AGE_SECONDS
    if (config is None or not config.event_webhook_public_key or not fresh
            or not verify_event_signature(config.event_webhook_public_key, signature, timestamp,
                                          body)):
        return JSONResponse({"error_code": "SIGNATURE_INVALID"}, status_code=403)
    try:
        events = json.loads(body)
    except ValueError:
        return JSONResponse({"error_code": "INVALID_JSON"}, status_code=422)
    if not isinstance(events, list):
        return JSONResponse({"error_code": "INVALID_JSON"}, status_code=422)
    acted = await handle_events(db, tenant_id, [e for e in events if isinstance(e, dict)])
    return JSONResponse({"acted": acted})

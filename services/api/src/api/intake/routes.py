"""Lead intake routes (A10).

Public webhooks -- the tenant comes ONLY from a signed route token (forms, the
notification mailbox) or from the Meta page id after Meta's app signature checks:
  POST /public/intake/forms/{token}   landing-page / form-tool submissions
  POST /public/intake/email/{token}   SendGrid Inbound Parse of forwarded lead notifications
  GET|POST /public/intake/meta        Meta Lead Ads webhook (platform app)
Admin (CLIENT_ADMIN; PLATFORM_ADMIN via /admin/tenants/{tenant_id}/intake):
  GET|PUT /admin/intake/config, GET /admin/intake/events, POST /admin/intake/events/{id}:resolve
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Callable
from typing import Any

from common.auth import AuthClaims, Role
from common.errors import NotFoundError, ValidationError
from common.logging import get_logger
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field, field_validator

from api.agents.dispatch import system_claims
from api.agents.events import emit
from api.audit.repository import record_audit
from api.auth.dependencies import get_platform_admin_actor, require_roles, resolve_tenant_scope
from api.config import get_api_settings
from api.email_agent.inbound import parse_raw, sendgrid_auth
from api.email_agent.links import link_secret, public_url
from api.intake import meta
from api.intake import repository as repo
from api.intake.parsers import (
    gmail_confirmation,
    label_values,
    normalise,
    notification_text,
    trusted_sender,
)
from api.intake.service import receive

_log = get_logger(__name__)

public_router = APIRouter(prefix="/public/intake", tags=["intake"])
router = APIRouter(prefix="/admin/intake", tags=["intake"])
tenant_scoped_router = APIRouter(prefix="/admin/tenants/{tenant_id}/intake", tags=["intake"])

_MAX_RAW_BYTES = 5 * 1024 * 1024
_MAX_FIELDS = 60


# -- Route tokens (the URL is the credential, as for SendGrid Inbound Parse) --------------


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def sign_intake(tenant_id: str, kind: str) -> str:
    payload = tenant_id.encode()
    mac = hmac.new(link_secret(), f"intake.{kind}.v1.".encode() + payload, hashlib.sha256)
    return f"{_b64(payload)}.{_b64(mac.digest())}"


def verify_intake(token: str, kind: str) -> str | None:
    try:
        payload_b64, mac_b64 = token.split(".")
        payload = base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4))
        mac = base64.urlsafe_b64decode(mac_b64 + "=" * (-len(mac_b64) % 4))
        tenant_id = payload.decode()
    except (ValueError, UnicodeDecodeError):
        return None
    expected = hmac.new(link_secret(), f"intake.{kind}.v1.".encode() + payload,
                        hashlib.sha256).digest()
    return tenant_id if tenant_id and hmac.compare_digest(mac, expected) else None


def _not_found() -> JSONResponse:
    return JSONResponse({"error_code": "NOT_FOUND"}, status_code=404)


# -- Public: forms --------------------------------------------------------------------------


@public_router.post("/forms/{token}")
async def form_submission(token: str, request: Request) -> JSONResponse:
    tenant_id = verify_intake(token, "form")
    if tenant_id is None:
        return _not_found()
    if "json" in request.headers.get("content-type", ""):
        try:
            body = await request.json()
        except ValueError:
            body = None
    else:
        body = dict(await request.form())
    if not isinstance(body, dict) or not body or len(body) > _MAX_FIELDS:
        return JSONResponse({"error_code": "FORM_FIELDS_REQUIRED"}, status_code=422)
    fields = {str(k)[:120]: str(v)[:2000] for k, v in body.items()
              if isinstance(v, str | int | float | bool)}
    lead = normalise(fields)
    external_id = lead.submission_id or hashlib.sha256(
        json.dumps(fields, sort_keys=True).encode()).hexdigest()
    status = await receive(request.app.state.db, system_claims(tenant_id), source="web_form",
                           external_id=external_id, lead=lead)
    return JSONResponse({"status": status})


# -- Public: forwarded lead-notification e-mails --------------------------------------------


@public_router.post("/email/{token}")
async def notification_email(token: str, request: Request) -> JSONResponse:
    """SendGrid Inbound Parse, raw MIME mode. 200 for anything handled (incl. rejects)."""
    tenant_id = verify_intake(token, "email")
    if tenant_id is None:
        return _not_found()
    form = await request.form(max_part_size=_MAX_RAW_BYTES)
    raw = form.get("email")
    if raw is None:
        return JSONResponse({"error_code": "RAW_MIME_REQUIRED"}, status_code=422)
    data = raw.encode() if isinstance(raw, str) else await raw.read()
    email = parse_raw(data)
    db, claims = request.app.state.db, system_claims(tenant_id)
    seen = {"from": email.from_addr, "subject": email.subject}

    code = gmail_confirmation(email.from_addr, email.subject)
    if code is not None:  # Gmail asks to confirm the forwarding address (I6)
        _, created = await repo.record_event(
            db, claims, source="meta_email", external_id=email.rfc_message_id, status="notice",
            reason="GMAIL_FORWARDING_CONFIRMATION", fields={**seen, "code": code})
        return JSONResponse({"status": "notice" if created else "duplicate"})

    config = await repo.get_config(db, claims)
    spf, dkim = form.get("SPF"), form.get("dkim")
    auth = sendgrid_auth(spf if isinstance(spf, str) else None,
                         dkim if isinstance(dkim, str) else None, email.from_addr)
    if not (trusted_sender(email.from_addr, list(config.trusted_senders)) and auth["dkim"]):
        _, created = await repo.record_event(
            db, claims, source="meta_email", external_id=email.rfc_message_id,
            status="rejected", reason="UNTRUSTED_SENDER", fields=seen)
        return JSONResponse({"status": "rejected" if created else "duplicate"})

    lead = normalise(label_values(notification_text(data)))
    lead.form = lead.form or email.subject[:200] or None
    status = await receive(db, claims, source="meta_email", external_id=email.rfc_message_id,
                           lead=lead)
    return JSONResponse({"status": status})


# -- Public: Meta Lead Ads webhook ------------------------------------------------------------


@public_router.get("/meta")
async def meta_subscribe(request: Request) -> Any:
    expected = get_api_settings().meta_verify_token
    params = request.query_params
    if not expected or not get_api_settings().meta_app_secret:
        return _not_found()
    if params.get("hub.mode") == "subscribe" and hmac.compare_digest(
            params.get("hub.verify_token", ""), expected):
        return PlainTextResponse(params.get("hub.challenge", ""))
    return JSONResponse({"error_code": "FORBIDDEN"}, status_code=403)


@public_router.post("/meta")
async def meta_webhook(request: Request) -> JSONResponse:
    secret = get_api_settings().meta_app_secret
    if not secret:
        return _not_found()
    raw = await request.body()
    if not meta.signature_ok(raw, request.headers.get("X-Hub-Signature-256"), secret):
        return JSONResponse({"error_code": "SIGNATURE_INVALID"}, status_code=401)
    try:
        body = json.loads(raw)
    except ValueError:
        return JSONResponse({"error_code": "BAD_PAYLOAD"}, status_code=422)
    db, queued = request.app.state.db, 0
    for entry in body.get("entry", []) if isinstance(body, dict) else []:
        for change in entry.get("changes", []):
            value = change.get("value") or {}
            if change.get("field") != "leadgen" or not value.get("leadgen_id"):
                continue
            tenant_id = await repo.tenant_for_page_system(db, str(value.get("page_id", "")))
            if tenant_id is None:
                _log.warning("meta lead for an unconnected page",
                             extra={"event": "intake_meta_unknown_page"})
                continue
            claims = system_claims(tenant_id)
            intake_id, created = await repo.record_event(
                db, claims, source="meta_api", external_id=str(value["leadgen_id"]),
                status="pending", fields={"form": value.get("form_id")})
            if created:
                await emit(db, claims, "intake_meta_lead", {"intake_id": intake_id})
                queued += 1
    return JSONResponse({"queued": queued})


# -- Admin --------------------------------------------------------------------------------


class IntakeConfigBody(BaseModel):
    enabled: bool = True
    trusted_senders: list[str] = Field(default_factory=lambda: list(repo.DEFAULT_TRUSTED),
                                       max_length=10)
    form_consent_wording: str | None = Field(default=None, max_length=2000)
    call_consent_wording: str | None = Field(default=None, max_length=2000)
    meta_page_id: str | None = Field(default=None, max_length=40, pattern=r"^\d+$")
    meta_page_token: str | None = Field(default=None, max_length=1000)  # write-only; "" clears

    @field_validator("trusted_senders")
    @classmethod
    def _domains(cls, value: list[str]) -> list[str]:
        cleaned = [d.strip().lower().lstrip("@") for d in value if d.strip()]
        for domain in cleaned:
            if "." not in domain or " " in domain or "/" in domain:
                raise ValueError(f"{domain!r} is not a domain.")
        return cleaned


def _config_out(config: repo.IntakeConfig, tenant_id: str) -> dict[str, Any]:
    settings = get_api_settings()
    return {
        "enabled": config.enabled, "trusted_senders": list(config.trusted_senders),
        "form_consent_wording": config.form_consent_wording,
        "call_consent_wording": config.call_consent_wording,
        "meta_page_id": config.meta_page_id, "has_meta_page_token": config.has_meta_page_token,
        "form_url": public_url(f"/public/intake/forms/{sign_intake(tenant_id, 'form')}"),
        "email_parse_url": public_url(f"/public/intake/email/{sign_intake(tenant_id, 'email')}"),
        "meta_webhook_url": (public_url("/public/intake/meta")
                             if settings.meta_app_secret and settings.meta_verify_token
                             else None),
    }


def _event_out(e: repo.IntakeEvent) -> dict[str, Any]:
    return {"intake_id": e.intake_id, "source": e.source, "status": e.status,
            "reason": e.reason, "lead_id": e.lead_id, "fields": e.fields,
            "received_at": e.received_at.isoformat(), "resolved_by": e.resolved_by}


def _mount(r: APIRouter, admin: Callable[..., Any]) -> None:

    @r.get("/config")
    async def get_config(request: Request,
                         claims: AuthClaims = Depends(admin)) -> dict[str, Any]:  # noqa: B008
        config = await repo.get_config(request.app.state.db, claims)
        return _config_out(config, str(claims.tenant_id))

    @r.put("/config")
    async def put_config(body: IntakeConfigBody, request: Request,
                         claims: AuthClaims = Depends(admin)) -> dict[str, Any]:  # noqa: B008
        if not body.trusted_senders:
            raise ValidationError("Add at least one trusted sender domain.",
                                  code="INTAKE_CONFIG_INVALID")
        db = request.app.state.db
        await repo.upsert_config(db, claims, page_token=body.meta_page_token, config=(
            repo.IntakeConfig(
                enabled=body.enabled, trusted_senders=tuple(body.trusted_senders),
                form_consent_wording=(body.form_consent_wording or "").strip() or None,
                call_consent_wording=(body.call_consent_wording or "").strip() or None,
                meta_page_id=body.meta_page_id or None)))
        await record_audit(db, claims, action="intake.config_updated",
                           target_type="tenant_lead_intake_configs", target_id=claims.tenant_id,
                           metadata={"page_token_changed": body.meta_page_token is not None,
                                     "call_consent_wording_set": bool(
                                         body.call_consent_wording)},
                           actor_context=get_platform_admin_actor(request))
        return _config_out(await repo.get_config(db, claims), str(claims.tenant_id))

    @r.get("/events")
    async def events(request: Request, queue: bool = True, limit: int = 50, offset: int = 0,
                     claims: AuthClaims = Depends(admin)) -> list[dict[str, Any]]:  # noqa: B008
        rows = await repo.list_events(request.app.state.db, claims, queue_only=queue,
                                      limit=limit, offset=offset)
        return [_event_out(e) for e in rows]

    @r.post("/events/{intake_id}:resolve")
    async def resolve(intake_id: str, request: Request,
                      claims: AuthClaims = Depends(admin)) -> dict[str, str]:  # noqa: B008
        db = request.app.state.db
        if not await repo.resolve_event(db, claims, intake_id):
            raise NotFoundError("Nothing to resolve.", code="INTAKE_EVENT_NOT_FOUND")
        await record_audit(db, claims, action="intake.event_resolved",
                           target_type="lead_intake_event", target_id=intake_id,
                           actor_context=get_platform_admin_actor(request))
        return {"intake_id": intake_id, "status": "resolved"}


_mount(router, require_roles(Role.CLIENT_ADMIN))
_mount(tenant_scoped_router, resolve_tenant_scope(Role.CLIENT_ADMIN))

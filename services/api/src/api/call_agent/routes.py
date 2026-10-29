"""Calling Agent routes (A6): admin config + call history, and Plivo's webhooks.

Webhooks carry no session: our HMAC ticket proves the call id is ours, then
``X-Plivo-Signature-V3`` is checked with THAT call's tenant auth token. Every
failure is the same 401, whichever check failed.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, Literal

from common.auth import AuthClaims, Role
from common.errors import NotFoundError, ValidationError
from common.phone import to_e164
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field

from api.agents.dispatch import system_claims
from api.agents.repository import contact_visible
from api.audit.repository import record_audit
from api.auth.dependencies import get_platform_admin_actor, require_roles, resolve_tenant_scope
from api.call_agent import repository as repo
from api.call_agent import service
from api.config import get_api_settings
from api.voice_agent.plivo import (
    NONCE_HEADER,
    SIGNATURE_HEADER,
    PlivoSignatureInvalidError,
    verify_signature,
    xml,
)

router = APIRouter(prefix="/admin/outreach", tags=["call-agent"])
tenant_scoped_router = APIRouter(prefix="/admin/tenants/{tenant_id}/outreach",
                                 tags=["call-agent"])
public_router = APIRouter(prefix="/public/call-agent", tags=["call-agent"])


class CallConfigBody(BaseModel):
    enabled: bool = True
    plivo_auth_id: str = Field(min_length=5, max_length=64)
    plivo_auth_token: str | None = Field(default=None, max_length=200)  # write-only
    from_number: str = Field(min_length=7, max_length=30)
    transfer_number: str | None = Field(default=None, max_length=30)
    company_name: str = Field(min_length=1, max_length=80)
    voice: str = Field(default="Polly.Joanna", max_length=60)
    voice_model: str | None = Field(default=None, max_length=100)
    voicemail_policy: Literal["hangup", "leave_message"] = "hangup"
    voicemail_message: str | None = Field(default=None, max_length=600)
    max_call_seconds: int = Field(default=300, ge=60, le=1800)
    max_concurrent_calls: int = Field(default=2, ge=1, le=20)
    recording_enabled: bool = True
    inbound_enabled: bool = True  # A7: answer calls to from_number
    recording_retention_days: int = Field(default=90, ge=1, le=3650)  # A9 H8


def _config_out(config: repo.CallConfig | None) -> dict[str, Any]:
    if config is None:
        return {"configured": False}
    base = (get_api_settings().public_api_base_url or "").rstrip("/")
    return {"configured": True, **config.__dict__, "has_auth_token": True,
            # A7: paste these into the Plivo Application attached to from_number.
            "inbound_answer_url": f"{base}/public/call-agent/inbound/answer",
            "inbound_hangup_url": f"{base}/public/call-agent/inbound/hangup"}


def _iso(row: dict[str, Any]) -> dict[str, Any]:
    return {k: v.isoformat() if hasattr(v, "isoformat") else v for k, v in row.items()}


def _mount(r: APIRouter, admin: Callable[..., Any], staff: Callable[..., Any]) -> None:

    @r.get("/call-config")
    async def get_config(request: Request,
                         claims: AuthClaims = Depends(staff)) -> dict[str, Any]:  # noqa: B008
        return _config_out(await repo.get_call_config(request.app.state.db, claims))

    @r.put("/call-config")
    async def put_config(body: CallConfigBody, request: Request,
                         claims: AuthClaims = Depends(admin)) -> dict[str, Any]:  # noqa: B008
        values = body.model_dump(exclude={"plivo_auth_token"})
        for key in ("from_number", "transfer_number"):
            if values[key]:
                values[key] = to_e164(values[key])
                if values[key] is None:
                    raise ValidationError(f"{key} is not a dialable number.",
                                          code="INVALID_PHONE")
        if body.voicemail_policy == "leave_message":
            if not body.voicemail_message:
                raise ValidationError("Leaving a voicemail needs the message.",
                                      code="VOICEMAIL_MESSAGE_REQUIRED")
            repo.validate_voicemail(body.voicemail_message, body.company_name)
        db = request.app.state.db
        await repo.register_number(db, claims, values["from_number"])  # A7 N1 (409 if taken)
        await repo.upsert_call_config(db, claims, values=values, auth_token=body.plivo_auth_token)
        await record_audit(db, claims, action="outreach.call_config_updated",
                           target_type="tenant_call_agent_configs", target_id=claims.tenant_id,
                           metadata={"token_changed": body.plivo_auth_token is not None,
                                     "recording_enabled": body.recording_enabled},
                           actor_context=get_platform_admin_actor(request))
        return _config_out(await repo.get_call_config(db, claims))

    @r.get("/calls")
    async def list_calls(request: Request, status: str | None = None, limit: int = 50,
                         offset: int = 0,
                         claims: AuthClaims = Depends(staff)) -> list[dict[str, Any]]:  # noqa: B008
        db = request.app.state.db
        rows = await repo.list_sessions(db, claims, status=status, limit=limit, offset=offset)
        return [_iso(r) for r in rows if await _can_see(db, claims, r["contact_id"])]

    @r.get("/calls/latency")
    async def latency(request: Request,
                      claims: AuthClaims = Depends(staff)) -> dict[str, Any]:  # noqa: B008
        return await repo.latency_stats(request.app.state.db, claims)

    @r.get("/calls/{call_id}")
    async def get_call(call_id: str, request: Request,
                       claims: AuthClaims = Depends(staff)) -> dict[str, Any]:  # noqa: B008
        db = request.app.state.db
        out = _iso(await _visible_call(db, claims, call_id))
        out["transcript"] = [_iso(t) for t in await repo.list_turns(db, claims, call_id)]
        return out

    @r.get("/calls/{call_id}/recording")
    async def recording(call_id: str, request: Request,
                        claims: AuthClaims = Depends(staff)) -> Response:  # noqa: B008
        db = request.app.state.db
        await _visible_call(db, claims, call_id)
        audio = await service.recording_audio(db, claims, call_id)
        return Response(audio, media_type="audio/mpeg", headers={"Cache-Control": "no-store"})


async def _can_see(db: Any, claims: AuthClaims, contact_id: str | None) -> bool:
    """A8 U7: agents see calls of their own contacts; unmatched callers are admin-only."""
    if contact_id is None:
        return claims.role is not Role.CLIENT_AGENT
    return await contact_visible(db, claims, contact_id)


async def _visible_call(db: Any, claims: AuthClaims, call_id: str) -> dict[str, Any]:
    rows = await repo.list_sessions(db, claims, status=None, limit=1, offset=0, call_id=call_id)
    if not rows or not await _can_see(db, claims, rows[0]["contact_id"]):
        raise NotFoundError("Call not found.", code="CALL_NOT_FOUND")
    return rows[0]


_mount(router, require_roles(Role.CLIENT_ADMIN), require_roles(Role.CLIENT_ADMIN,
                                                                Role.CLIENT_AGENT))
_platform = resolve_tenant_scope(Role.CLIENT_ADMIN)
_mount(tenant_scoped_router, _platform, _platform)


# -- Plivo webhooks ------------------------------------------------------------------------


async def _verified(request: Request, call_id: str) -> tuple[
        repo.CallSessionRow, dict[str, str], AuthClaims]:
    if not service.verify_ticket(call_id, request.query_params.get("ticket")):
        raise PlivoSignatureInvalidError()
    db = request.app.state.db
    session = await repo.get_session_system(db, call_id)
    if session is None:
        raise PlivoSignatureInvalidError()
    token = await repo.get_auth_token(db, session.tenant_id)
    form = await request.form()
    params = {key: str(value) for key, value in form.items()}
    base = (get_api_settings().public_api_base_url or "").rstrip("/")
    url = f"{base}{request.url.path}" + (f"?{request.url.query}" if request.url.query else "")
    verify_signature(url=url, params=params, auth_token=token or "",
                     signature=request.headers.get(SIGNATURE_HEADER),
                     nonce=request.headers.get(NONCE_HEADER))
    return session, params, system_claims(session.tenant_id)


@public_router.post("/calls/{call_id}/answer")
async def answer(call_id: str, request: Request) -> Response:
    session, params, claims = await _verified(request, call_id)
    return xml(await service.answer_xml(request.app.state.db, claims, session,
                                        params.get("CallUUID") or None))


@public_router.post("/calls/{call_id}/turn")
async def turn(call_id: str, request: Request) -> Response:
    started = time.monotonic()
    session, params, claims = await _verified(request, call_id)
    db = request.app.state.db
    speech = (params.get("Speech") or "").strip()
    if speech:
        await repo.add_turn(db, claims, call_id, speaker="caller", text=speech)
    result = await service.handle_turn(db, claims, session, speech)
    spoken = service.spoken_text(result.xml)
    if spoken:
        await repo.add_turn(db, claims, call_id, speaker="rebecca", text=spoken,
                            latency_ms=int((time.monotonic() - started) * 1000))
    return xml(result.xml)


@public_router.post("/calls/{call_id}/machine")
async def machine(call_id: str, request: Request) -> Response:
    session, params, claims = await _verified(request, call_id)
    if params.get("Machine", "").lower() == "true":
        await service.on_machine(request.app.state.db, claims, session)
    return Response(status_code=200)


@public_router.post("/calls/{call_id}/voicemail")
async def voicemail(call_id: str, request: Request) -> Response:
    _, _, claims = await _verified(request, call_id)
    return xml(await service.voicemail_xml(request.app.state.db, claims))


@public_router.post("/calls/{call_id}/dial-status")
async def dial_status(call_id: str, request: Request) -> Response:
    session, params, claims = await _verified(request, call_id)
    if params.get("DialStatus") == "completed":
        return xml("<Hangup/>")
    db = request.app.state.db
    body = await service.transfer_missed(db, claims, session)
    await repo.add_turn(db, claims, call_id, speaker="rebecca", text=service.spoken_text(body))
    return xml(body)


@public_router.post("/calls/{call_id}/whisper")
async def whisper(call_id: str, request: Request) -> Response:
    """Played to the team member who answers a transfer, before the caller joins (A7 N6)."""
    session, _, claims = await _verified(request, call_id)
    return xml(await service.whisper_xml(request.app.state.db, claims, session))


# -- Inbound (A7): static URLs on the Plivo Application attached to the number ----------


def _plivo_number(value: str) -> str | None:
    """Plivo posts full international numbers WITHOUT '+' ("912264233234"). ``to_e164``
    only reads US-shaped digits that way (it serves typed-in numbers), so a non-US number
    resolved to nothing and every call to it was rejected."""
    digits = "".join(ch for ch in value if ch.isdigit())
    return f"+{digits}" if 8 <= len(digits) <= 15 else to_e164(value)


async def _verified_inbound(request: Request) -> tuple[dict[str, str], AuthClaims]:
    """Tenant from the called number, then Plivo's signature with THAT tenant's token."""
    db = request.app.state.db
    form = await request.form()
    params = {key: str(value) for key, value in form.items()}
    called = _plivo_number(params.get("To", ""))
    tenant_id = await repo.tenant_for_number_system(db, called) if called else None
    if tenant_id is None:
        raise PlivoSignatureInvalidError()
    token = await repo.get_auth_token(db, tenant_id)
    base = (get_api_settings().public_api_base_url or "").rstrip("/")
    url = f"{base}{request.url.path}" + (f"?{request.url.query}" if request.url.query else "")
    verify_signature(url=url, params=params, auth_token=token or "",
                     signature=request.headers.get(SIGNATURE_HEADER),
                     nonce=request.headers.get(NONCE_HEADER))
    return params, system_claims(tenant_id)


@public_router.post("/inbound/answer")
async def inbound_answer(request: Request) -> Response:
    params, claims = await _verified_inbound(request)
    caller = _plivo_number(params.get("From", "")) or params.get("From", "unknown")
    return xml(await service.inbound_answer(
        request.app.state.db, claims, caller=caller, called=_plivo_number(params["To"]) or "",
        call_uuid=params.get("CallUUID") or None))


@public_router.post("/inbound/hangup")
async def inbound_hangup(request: Request) -> Response:
    params, claims = await _verified_inbound(request)
    db = request.app.state.db
    session = await repo.session_by_call_uuid(db, claims, params.get("CallUUID", ""))
    if session is not None:
        await service.on_hangup(db, claims, session, params)
    return Response(status_code=200)


@public_router.post("/calls/{call_id}/hangup")
async def hangup(call_id: str, request: Request) -> Response:
    session, params, claims = await _verified(request, call_id)
    await service.on_hangup(request.app.state.db, claims, session, params)
    return Response(status_code=200)

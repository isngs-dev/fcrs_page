"""Public routes for the AI voice agent ("Call Us"). See ``api.voice_agent.agent``.

``POST /public/voice-agent/calls``                        visitor session -> browser JWT + ticket
``GET  /public/voice-agent/calls/{call_id}``              visitor session -> call outcome
``POST /public/voice-agent/answer``                       Plivo app answer URL -> greeting + listen
``POST /public/voice-agent/calls/{call_id}/turn``         Plivo (GetInput) -> reply/transfer/bye
``POST /public/voice-agent/calls/{call_id}/dial-status``  Plivo (Dial action) -> transfer
``POST /public/voice-agent/hangup``                       Plivo app hangup URL -> close the call row

Plivo requests carry no session: ``X-Plivo-Signature-V3`` (platform auth
token, over the public URL + body) plus our HMAC call ticket are their auth. A
bad signature or ticket gets the same 401 as an unknown call.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from xml.sax.saxutils import escape, quoteattr

import httpx
from common.auth import AuthClaims, Role
from common.errors import InternalServerError, NotFoundError, ValidationError
from common.logging import get_logger
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel

from api.config import ApiSettings, get_api_settings
from api.conversation_store.repository import append_message, create_conversation, get_conversation
from api.gateway.dependencies import get_visitor_claims
from api.ratelimit import client_ip, enforce_rate_limit
from api.voice_agent.agent import (
    AFTER_HOURS_LINE,
    DEFAULT_GREETING,
    ERROR_LINE,
    GOODBYE_LINE,
    REASON_TEXT,
    SILENCE_LINE,
    TRANSFER_LINE,
    CallSession,
    answer_from_knowledge,
    call_ticket,
    in_business_hours,
    verify_call_ticket,
)
from api.voice_agent.plivo import (
    NONCE_HEADER,
    SIGNATURE_HEADER,
    PlivoSignatureInvalidError,
    listen,
    mint_browser_token,
    speak,
    verify_signature,
    xml,
)
from api.voice_agent.repository import (
    VoiceAgentCall,
    VoiceAgentConfig,
    create_voice_agent_call,
    get_voice_agent_call,
    get_voice_agent_call_by_plivo_uuid,
    get_voice_agent_config,
    get_voice_agent_config_by_tenant_id,
    save_voice_agent_session,
    update_voice_agent_call,
)

router = APIRouter(prefix="/public/voice-agent", tags=["voice-agent"])
_log = get_logger(__name__)

VOICE_INTENT = "voice_call"  # tags call turns in the conversation transcript
TICKET_PARAM = "X-PH-Ticket"  # the browser's extra SIP header, as Plivo posts it
NO_ANSWER_LINE = (
    "Sorry, our team couldn't pick up right now. I've opened the booking form in the "
    "chat so you can schedule a call. Goodbye."
)


def voice_agent_ready(settings: ApiSettings) -> bool:
    """Platform Plivo set up for browser calls (see config.py)."""
    return all((
        settings.public_api_base_url,
        settings.platform_plivo_auth_id,
        settings.platform_plivo_auth_token,
        settings.platform_plivo_from_number,
        settings.platform_plivo_app_id,
        settings.platform_plivo_endpoint_username,
    ))


def _base(settings: ApiSettings) -> str:
    return (settings.public_api_base_url or "").rstrip("/")


def _call_url(settings: ApiSettings, call_id: str, action: str) -> str:
    ticket = call_ticket(call_id)
    return f"{_base(settings)}/public/voice-agent/calls/{call_id}/{action}?ticket={ticket}"


def _say(text: str, settings: ApiSettings) -> str:
    return speak(text, settings.voice_agent_tts_voice)


def _listen(text: str, settings: ApiSettings, call_id: str) -> Response:
    return xml(listen(text, _call_url(settings, call_id, "turn"), settings.voice_agent_tts_voice))


def _handoff_note(reason: str) -> str:
    return f"[Call handed to the team: {REASON_TEXT[reason]}]"


def _visitor_claims(call: VoiceAgentCall) -> AuthClaims:
    return AuthClaims(subject=call.visitor_id, role=Role.VISITOR, tenant_id=call.tenant_id)


async def _record(request: Request, call: VoiceAgentCall, role: str, text: str) -> None:
    """Append one call turn to the visitor's conversation (the admin transcript).
    Best-effort: a store hiccup must not drop a live call."""
    try:
        await append_message(
            request.app.state.db, _visitor_claims(call), call.conversation_id,
            role=role, content=text, intent=VOICE_INTENT,
        )
    except Exception:  # noqa: BLE001
        _log.exception("voice_agent_transcript_write_failed", extra={"call_id": call.call_id})


async def _verified_params(request: Request) -> dict[str, str]:
    """Plivo signature over the public URL + form body -> the form params."""
    settings = get_api_settings()
    if not voice_agent_ready(settings):
        raise PlivoSignatureInvalidError()
    form = await request.form()
    params = {key: str(value) for key, value in form.items()}
    url = _base(settings) + request.url.path
    if request.url.query:
        url += f"?{request.url.query}"
    verify_signature(
        url=url,
        params=params,
        auth_token=settings.platform_plivo_auth_token or "",
        signature=request.headers.get(SIGNATURE_HEADER),
        nonce=request.headers.get(NONCE_HEADER),
    )
    return params


async def _verified_plivo(
    request: Request, call_id: str | None = None,
) -> tuple[VoiceAgentCall, dict[str, str]]:
    """Plivo signature + call ticket -> the call row and the form params."""
    params = await _verified_params(request)
    ticket = request.query_params.get("ticket") or params.get(TICKET_PARAM)
    ticket_call_id = verify_call_ticket(ticket)
    if ticket_call_id is None or (call_id is not None and ticket_call_id != call_id):
        raise PlivoSignatureInvalidError()
    call = await get_voice_agent_call(request.app.state.db, ticket_call_id)
    if call is None:
        raise PlivoSignatureInvalidError()
    return call, params


# -- visitor ------------------------------------------------------------------


class StartCallRequest(BaseModel):
    conversation_id: str | None = None


class StartCallResponse(BaseModel):
    call_id: str
    token: str
    ticket: str
    conversation_id: str


@router.post("/calls")
async def start_call(
    body: StartCallRequest,
    request: Request,
    claims: AuthClaims = Depends(get_visitor_claims),  # noqa: B008
) -> StartCallResponse:
    settings = get_api_settings()
    # Each call costs Plivo + LLM minutes: capped per visitor (and per IP) an hour.
    limits = (("voice_agent_visitor", claims.subject), ("voice_agent_ip", client_ip(request)))
    for scope, identifier in limits:
        await enforce_rate_limit(
            request, scope=scope, identifier=identifier,
            limit=settings.voice_agent_calls_per_hour, window_seconds=3600,
        )
    db = request.app.state.db
    config = await get_voice_agent_config(db, claims)
    if not config.enabled or not voice_agent_ready(settings):
        raise ValidationError("Calls aren't available right now.", code="VOICE_AGENT_UNAVAILABLE")
    conversation_id = body.conversation_id
    if conversation_id is None or await get_conversation(db, claims, conversation_id) is None:
        conversation_id = await create_conversation(db, claims)
    call_id = await create_voice_agent_call(db, claims, conversation_id=conversation_id)
    try:
        token = await mint_browser_token(
            auth_id=settings.platform_plivo_auth_id or "",
            auth_token=settings.platform_plivo_auth_token or "",
            endpoint_username=settings.platform_plivo_endpoint_username or "",
            app_id=settings.platform_plivo_app_id or "",
        )
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        await update_voice_agent_call(db, call_id, status="failed", ended=True)
        # Plivo's own error text (e.g. bad credentials, unknown endpoint) --
        # never contains our secrets. Only allowlisted log extras survive.
        reason = exc.__class__.__name__
        if isinstance(exc, httpx.HTTPStatusError):
            reason = f"plivo {exc.response.status_code}: {exc.response.text[:300]}"
        _log.warning(
            "voice_agent_token_failed",
            extra={"event": "voice_agent_token_failed", "reason": reason},
        )
        raise InternalServerError(
            "Calls aren't available right now.", code="VOICE_AGENT_TOKEN_FAILED",
        ) from exc
    return StartCallResponse(
        call_id=call_id, token=token, ticket=call_ticket(call_id), conversation_id=conversation_id,
    )


class CallStatusResponse(BaseModel):
    status: str
    # The visitor couldn't reach the team -> the widget opens the booking card.
    offer_booking: bool


@router.get("/calls/{call_id}")
async def call_status(
    call_id: str,
    request: Request,
    claims: AuthClaims = Depends(get_visitor_claims),  # noqa: B008
) -> CallStatusResponse:
    call = await get_voice_agent_call(request.app.state.db, call_id)
    if call is None or call.tenant_id != claims.tenant_id or call.visitor_id != claims.subject:
        raise NotFoundError("Call not found.", code="CALL_NOT_FOUND")
    return CallStatusResponse(
        status=call.status, offer_booking=call.status in ("after_hours", "transfer_unanswered"),
    )


# -- Plivo --------------------------------------------------------------------


@router.post("/answer")
async def answer(request: Request) -> Response:
    """The Plivo Application's answer URL: greet the caller and listen."""
    call, params = await _verified_plivo(request)
    settings = get_api_settings()
    db = request.app.state.db
    config = await get_voice_agent_config_by_tenant_id(db, call.tenant_id)
    if not config.enabled:
        await update_voice_agent_call(db, call.call_id, status="failed", ended=True)
        unavailable = _say("Sorry, calls aren't available right now. Goodbye.", settings)
        return xml(unavailable + "<Hangup/>")
    await update_voice_agent_call(
        db, call.call_id, status="in_progress",
        plivo_call_uuid=params.get("CallUUID") or None, started=True,
    )
    greeting = config.greeting or DEFAULT_GREETING
    await _record(request, call, "bot", greeting)
    return _listen(greeting, settings, call.call_id)


@router.post("/calls/{call_id}/turn")
async def turn(call_id: str, request: Request) -> Response:
    """One caller utterance (``Speech``, empty on silence) -> what to say/do next."""
    call, params = await _verified_plivo(request, call_id)
    db = request.app.state.db
    config = await get_voice_agent_config_by_tenant_id(db, call.tenant_id)
    started = (call.started_at or datetime.now(UTC)).timestamp()
    session = CallSession.from_state(call.session, max_minutes=config.max_minutes, started=started)
    prompt = (params.get("Speech") or "").strip()
    response = await _next_step(request, call, config, session, prompt)
    await save_voice_agent_session(db, call_id, session.to_state())
    return response


async def _next_step(
    request: Request, call: VoiceAgentCall, config: VoiceAgentConfig, session: CallSession,
    prompt: str,
) -> Response:
    settings = get_api_settings()
    db = request.app.state.db
    call_id = call.call_id

    async def hang_up(say: str, status: str, reason: str | None = None) -> Response:
        await _record(request, call, "bot", say)
        if reason:
            await _record(request, call, "bot", _handoff_note(reason))
        await update_voice_agent_call(
            db, call_id, status=status, transfer_reason=reason, ended=True,
        )
        return xml(_say(say, settings) + "<Hangup/>")

    if not prompt:
        if session.on_silence():
            return await hang_up(GOODBYE_LINE, "completed")
        return _listen(SILENCE_LINE, settings, call_id)

    await _record(request, call, "user", prompt)
    action, reason = session.before_answer(prompt)
    line = TRANSFER_LINE
    if action == "goodbye":
        return await hang_up(GOODBYE_LINE, "completed")
    if action == "answer":
        try:
            reply = await answer_from_knowledge(db, _visitor_claims(call), prompt, session.history)
        except Exception:  # noqa: BLE001 -- any failure hands the caller to a person
            _log.exception("voice_agent_answer_failed", extra={"call_id": call_id})
            line, reason = f"{ERROR_LINE} {TRANSFER_LINE}", "agent_error"
        else:
            line, reason = session.after_answer(prompt, reply)
    if reason is None:
        await _record(request, call, "bot", line)
        return _listen(line, settings, call_id)

    # A caller asking for a person, or a question the agent can't answer, always
    # goes to the saved number (an unanswered dial still offers the booking
    # form); the agent's own hand-offs (repeated question, time limit) respect
    # business hours.
    always_dial = reason in ("asked_for_person", "could_not_answer", "agent_error")
    if not always_dial and not in_business_hours(config, datetime.now(UTC)):
        return await hang_up(AFTER_HOURS_LINE, "after_hours", reason)
    await _record(request, call, "bot", line)
    await _record(request, call, "bot", _handoff_note(reason))
    await update_voice_agent_call(db, call_id, status="transferring", transfer_reason=reason)
    return xml(
        _say(line, settings)
        + f"<Dial callerId={quoteattr(settings.platform_plivo_from_number or '')}"
        f' timeout="25" action={quoteattr(_call_url(settings, call_id, "dial-status"))}'
        f' method="POST"><Number>{escape(config.transfer_number)}</Number></Dial>'
    )


@router.post("/calls/{call_id}/dial-status")
async def dial_status(call_id: str, request: Request) -> Response:
    call, params = await _verified_plivo(request, call_id)
    settings = get_api_settings()
    answered = params.get("DialStatus") == "completed"
    await update_voice_agent_call(
        request.app.state.db, call_id,
        status="transferred" if answered else "transfer_unanswered", ended=True,
    )
    if answered:
        return xml("<Hangup/>")
    await _record(request, call, "bot", NO_ANSWER_LINE)
    return xml(_say(NO_ANSWER_LINE, settings) + "<Hangup/>")


@router.post("/hangup")
async def hangup(request: Request) -> Response:
    """The Plivo Application's hangup URL: close a call the caller hung up on."""
    params = await _verified_params(request)
    db = request.app.state.db
    call_id = verify_call_ticket(params.get(TICKET_PARAM))
    call: Any = None
    if call_id is not None:
        call = await get_voice_agent_call(db, call_id)
    elif params.get("CallUUID"):
        call = await get_voice_agent_call_by_plivo_uuid(db, params["CallUUID"])
    if call is not None and call.status in ("starting", "in_progress"):
        status = "completed" if call.status == "in_progress" else "failed"
        await update_voice_agent_call(db, call.call_id, status=status, ended=True)
    return Response(status_code=200)

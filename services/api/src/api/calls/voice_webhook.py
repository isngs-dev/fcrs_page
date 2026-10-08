"""Twilio webhooks for the AI voice confirmation call -- public, signature-authenticated.

``POST /public/calls/voice/{tenant_id}/{call_id}/start``           -> TwiML: intro + question 0
``POST /public/calls/voice/{tenant_id}/{call_id}/answer/{index}``  -> store answer, next question
``POST /public/calls/voice/{tenant_id}/{call_id}/status``          -> terminal call status

No session: ``X-Twilio-Signature`` (verified with the same Twilio account's
Auth Token that placed the call, see ``voice.twilio_credentials_for``) is the
auth, exactly like ``api.calls.webhook``. The signed URL is rebuilt from
``public_api_base_url`` -- the URL Twilio was given -- not from the request,
which arrives via the reverse proxy on an internal http address. An unknown
tenant/call or a bad signature gets the same 401, no distinguishing signal.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response

from api.calls.voice import (
    goodbye_twiml,
    parse_yes_no,
    question_twiml,
    twilio_credentials_for,
)
from api.calls.voice_repository import (
    VoiceCall,
    get_voice_call_by_id,
    record_voice_call_answer,
    set_voice_call_status,
)
from api.calls.webhook import _SIGNATURE_HEADER, TwilioSignatureInvalidError, _verify_signature
from api.config import get_api_settings

router = APIRouter(prefix="/public/calls/voice", tags=["calls"])

# Twilio CallStatus -> our terminal status.
_TERMINAL_STATUSES = {
    "completed": "completed",
    "no-answer": "no_answer",
    "busy": "busy",
    "failed": "failed",
    "canceled": "failed",
}


async def _verified_call(
    request: Request, tenant_id: str, call_id: str,
) -> tuple[VoiceCall, dict[str, str]]:
    db = request.app.state.db
    settings = get_api_settings()
    creds = await twilio_credentials_for(db, tenant_id)
    call = await get_voice_call_by_id(db, tenant_id, call_id)
    if creds is None or call is None or not settings.public_api_base_url:
        raise TwilioSignatureInvalidError()
    form = await request.form()
    params = {key: str(value) for key, value in form.items()}
    url = settings.public_api_base_url.rstrip("/") + request.url.path
    if request.url.query:
        url += f"?{request.url.query}"
    _verify_signature(
        url=url,
        params=params,
        auth_token=creds.auth_token,
        header_value=request.headers.get(_SIGNATURE_HEADER),
    )
    return call, params


def _twiml_for(call: VoiceCall, index: int) -> Response:
    settings = get_api_settings()
    voice = settings.voice_call_voice
    if index >= len(call.questions):
        return Response(content=goodbye_twiml(voice), media_type="application/xml")
    base = f"{(settings.public_api_base_url or '').rstrip('/')}/public/calls/voice"
    action = f"{base}/{call.tenant_id}/{call.call_id}/answer/{index}"
    return Response(
        content=question_twiml(
            question=call.questions[index], action_url=action, voice=voice, intro=index == 0,
        ),
        media_type="application/xml",
    )


@router.post("/{tenant_id}/{call_id}/start")
async def voice_call_start(tenant_id: str, call_id: str, request: Request) -> Response:
    call, _ = await _verified_call(request, tenant_id, call_id)
    return _twiml_for(call, 0)


@router.post("/{tenant_id}/{call_id}/answer/{index}")
async def voice_call_answer(tenant_id: str, call_id: str, index: int, request: Request) -> Response:
    call, params = await _verified_call(request, tenant_id, call_id)
    if 0 <= index < len(call.questions):
        speech = params.get("SpeechResult")
        digits = params.get("Digits")
        entry: dict[str, Any] = {
            "question": call.questions[index],
            "heard": speech or (f"pressed {digits}" if digits else ""),
            "answer": parse_yes_no(speech, digits),
        }
        await record_voice_call_answer(
            request.app.state.db, tenant_id, call_id, index=index, entry=entry,
        )
    return _twiml_for(call, index + 1)


@router.post("/{tenant_id}/{call_id}/status", status_code=200)
async def voice_call_status(tenant_id: str, call_id: str, request: Request) -> Response:
    call, params = await _verified_call(request, tenant_id, call_id)
    status = _TERMINAL_STATUSES.get(params.get("CallStatus", ""))
    if status is not None:
        await set_voice_call_status(
            request.app.state.db,
            tenant_id,
            call.call_id,
            status=status,
            twilio_call_sid=params.get("CallSid") or None,
        )
    return Response(status_code=200)

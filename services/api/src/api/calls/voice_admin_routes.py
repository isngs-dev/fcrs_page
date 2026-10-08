"""Admin routes for AI voice confirmation calls.

``GET/PUT /admin/calls/voice-config`` -- CLIENT_ADMIN (configuration).
``GET /admin/calls/leads/{lead_id}/voice-calls`` -- CLIENT_ADMIN/CLIENT_AGENT
(lead review, like the leads console). Each has the usual
``/admin/tenants/{tenant_id}/...`` PLATFORM_ADMIN twin (``resolve_tenant_scope``).
"""
from __future__ import annotations

from datetime import datetime

from common.auth import AuthClaims, Role
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, field_validator

from api.auth.dependencies import require_roles, resolve_tenant_scope
from api.calls.voice_repository import (
    DEFAULT_QUESTIONS,
    get_voice_call_config,
    list_voice_calls_for_lead,
    upsert_voice_call_config,
)

router = APIRouter(prefix="/admin/calls", tags=["calls"])
tenant_scoped_router = APIRouter(prefix="/admin/tenants/{tenant_id}/calls", tags=["calls"])

MAX_QUESTIONS = 5
MAX_QUESTION_LENGTH = 300


class VoiceCallConfigBody(BaseModel):
    enabled: bool = False
    questions: list[str]

    @field_validator("questions")
    @classmethod
    def _validate_questions(cls, v: list[str]) -> list[str]:
        cleaned = [q.strip() for q in v if q.strip()]
        if not cleaned:
            raise ValueError("at least one question is required")
        if len(cleaned) > MAX_QUESTIONS:
            raise ValueError(f"at most {MAX_QUESTIONS} questions")
        if any(len(q) > MAX_QUESTION_LENGTH for q in cleaned):
            raise ValueError(f"each question must be at most {MAX_QUESTION_LENGTH} characters")
        return cleaned


class TranscriptEntry(BaseModel):
    question: str
    heard: str
    answer: str


class VoiceCallResponse(BaseModel):
    call_id: str
    status: str
    to_number: str
    created_at: datetime
    last_error: str | None
    transcript: list[TranscriptEntry]


async def _get_config(request: Request, claims: AuthClaims) -> VoiceCallConfigBody:
    config = await get_voice_call_config(request.app.state.db, claims)
    if config is None:
        return VoiceCallConfigBody(enabled=False, questions=list(DEFAULT_QUESTIONS))
    return VoiceCallConfigBody(enabled=config.enabled, questions=config.questions)


async def _put_config(
    request: Request, claims: AuthClaims, body: VoiceCallConfigBody,
) -> VoiceCallConfigBody:
    await upsert_voice_call_config(
        request.app.state.db, claims, enabled=body.enabled, questions=body.questions,
    )
    return body


async def _list_calls(
    request: Request, claims: AuthClaims, lead_id: str,
) -> list[VoiceCallResponse]:
    calls = await list_voice_calls_for_lead(request.app.state.db, claims, lead_id)
    return [
        VoiceCallResponse(
            call_id=c.call_id,
            status=c.status,
            to_number=c.to_number,
            created_at=c.created_at,
            last_error=c.last_error,
            transcript=[TranscriptEntry(**entry) for entry in c.transcript],
        )
        for c in calls
    ]


@router.get("/voice-config")
async def get_voice_config(
    request: Request,
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN)),  # noqa: B008
) -> VoiceCallConfigBody:
    return await _get_config(request, claims)


@router.put("/voice-config")
async def put_voice_config(
    body: VoiceCallConfigBody,
    request: Request,
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN)),  # noqa: B008
) -> VoiceCallConfigBody:
    return await _put_config(request, claims, body)


@router.get("/leads/{lead_id}/voice-calls")
async def get_lead_voice_calls(
    lead_id: str,
    request: Request,
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN, Role.CLIENT_AGENT)),  # noqa: B008
) -> list[VoiceCallResponse]:
    return await _list_calls(request, claims, lead_id)


@tenant_scoped_router.get("/voice-config")
async def get_voice_config_for_tenant(
    request: Request,
    claims: AuthClaims = Depends(resolve_tenant_scope(Role.CLIENT_ADMIN)),  # noqa: B008
) -> VoiceCallConfigBody:
    return await _get_config(request, claims)


@tenant_scoped_router.put("/voice-config")
async def put_voice_config_for_tenant(
    body: VoiceCallConfigBody,
    request: Request,
    claims: AuthClaims = Depends(resolve_tenant_scope(Role.CLIENT_ADMIN)),  # noqa: B008
) -> VoiceCallConfigBody:
    return await _put_config(request, claims, body)


@tenant_scoped_router.get("/leads/{lead_id}/voice-calls")
async def get_lead_voice_calls_for_tenant(
    lead_id: str,
    request: Request,
    claims: AuthClaims = Depends(resolve_tenant_scope(Role.CLIENT_ADMIN)),  # noqa: B008
) -> list[VoiceCallResponse]:
    return await _list_calls(request, claims, lead_id)

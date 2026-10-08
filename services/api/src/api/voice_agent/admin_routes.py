"""Admin routes for the AI voice agent ("Call Us").

``GET/PUT /admin/voice-agent/config`` -- CLIENT_ADMIN, with the usual
``/admin/tenants/{tenant_id}/voice-agent/config`` PLATFORM_ADMIN twin.
"""
from __future__ import annotations

import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from common.auth import AuthClaims, Role
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, field_validator

from api.auth.dependencies import require_roles, resolve_tenant_scope
from api.calls.voice import to_e164
from api.config import get_api_settings
from api.voice_agent.repository import (
    DEFAULT_TRANSFER_NUMBER,
    VoiceAgentConfig,
    get_voice_agent_config,
    upsert_voice_agent_config,
)
from api.voice_agent.routes import voice_agent_ready

router = APIRouter(prefix="/admin/voice-agent", tags=["voice-agent"])
tenant_scoped_router = APIRouter(
    prefix="/admin/tenants/{tenant_id}/voice-agent", tags=["voice-agent"],
)

_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class VoiceAgentConfigBody(BaseModel):
    enabled: bool = False
    greeting: str | None = Field(default=None, max_length=400)
    transfer_number: str = DEFAULT_TRANSFER_NUMBER
    timezone: str = "America/New_York"
    open_time: str = "09:00"
    close_time: str = "18:00"
    open_days: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4])
    max_minutes: int = Field(default=10, ge=1, le=60)

    @field_validator("greeting")
    @classmethod
    def _greeting(cls, v: str | None) -> str | None:
        return (v or "").strip() or None

    @field_validator("transfer_number")
    @classmethod
    def _number(cls, v: str) -> str:
        number = to_e164(v)
        if number is None:
            raise ValueError("enter a full phone number, e.g. (786) 823-3553 or +17868233553")
        return number

    @field_validator("timezone")
    @classmethod
    def _timezone(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("unknown time zone") from exc
        return v

    @field_validator("open_time", "close_time")
    @classmethod
    def _time(cls, v: str) -> str:
        if not _HHMM.match(v):
            raise ValueError("use 24-hour HH:MM")
        return v

    @field_validator("open_days")
    @classmethod
    def _days(cls, v: list[int]) -> list[int]:
        if any(d < 0 or d > 6 for d in v):
            raise ValueError("days are 0 (Monday) to 6 (Sunday)")
        return sorted(set(v))


class VoiceAgentConfigResponse(VoiceAgentConfigBody):
    # Platform Plivo set up for browser calls -- without it "Call Us" stays
    # hidden even when enabled, so the console says why.
    plivo_ready: bool


async def _get(request: Request, claims: AuthClaims) -> VoiceAgentConfigResponse:
    c = await get_voice_agent_config(request.app.state.db, claims)
    return VoiceAgentConfigResponse(
        enabled=c.enabled, greeting=c.greeting, transfer_number=c.transfer_number,
        timezone=c.timezone, open_time=c.open_time, close_time=c.close_time,
        open_days=list(c.open_days), max_minutes=c.max_minutes,
        plivo_ready=voice_agent_ready(get_api_settings()),
    )


async def _put(
    request: Request, claims: AuthClaims, body: VoiceAgentConfigBody,
) -> VoiceAgentConfigResponse:
    await upsert_voice_agent_config(
        request.app.state.db, claims,
        VoiceAgentConfig(
            enabled=body.enabled, greeting=body.greeting, transfer_number=body.transfer_number,
            timezone=body.timezone, open_time=body.open_time, close_time=body.close_time,
            open_days=tuple(body.open_days), max_minutes=body.max_minutes,
        ),
    )
    return await _get(request, claims)


@router.get("/config")
async def get_config(
    request: Request,
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN)),  # noqa: B008
) -> VoiceAgentConfigResponse:
    return await _get(request, claims)


@router.put("/config")
async def put_config(
    body: VoiceAgentConfigBody,
    request: Request,
    claims: AuthClaims = Depends(require_roles(Role.CLIENT_ADMIN)),  # noqa: B008
) -> VoiceAgentConfigResponse:
    return await _put(request, claims, body)


@tenant_scoped_router.get("/config")
async def get_config_for_tenant(
    request: Request,
    claims: AuthClaims = Depends(resolve_tenant_scope(Role.CLIENT_ADMIN)),  # noqa: B008
) -> VoiceAgentConfigResponse:
    return await _get(request, claims)


@tenant_scoped_router.put("/config")
async def put_config_for_tenant(
    body: VoiceAgentConfigBody,
    request: Request,
    claims: AuthClaims = Depends(resolve_tenant_scope(Role.CLIENT_ADMIN)),  # noqa: B008
) -> VoiceAgentConfigResponse:
    return await _put(request, claims, body)

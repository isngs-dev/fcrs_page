"""Outreach admin API (A1.2/A1.4): settings, kill switches, suppressions, consents,
gate dry-run, plans.

Implicit routes (``/admin/outreach``) serve CLIENT_ADMIN/CLIENT_AGENT; the same
handlers mount under ``/admin/tenants/{tenant_id}/outreach`` for PLATFORM_ADMIN
(``resolve_tenant_scope``). CLIENT_AGENT reaches only contacts they own or whose
lead is assigned to them -- anything else is 404, not 403.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, time
from typing import Any, Literal

from common.auth import AuthClaims, Role
from common.errors import ConflictError, NotFoundError, ValidationError
from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, Field, model_validator

from api.agents import compliance
from api.agents.events import emit, get_event_status
from api.agents.models import PlanKind
from api.agents.outreach_repository import get_active_plan, get_plan_contact_id, stop_plan
from api.agents.repository import (
    AgentSettings,
    add_consent,
    add_suppression,
    contact_visible,
    delete_suppression,
    finish_run,
    get_agent_settings,
    list_consents,
    list_suppressions,
    set_platform_paused,
    set_tenant_paused,
    start_run,
    upsert_agent_settings,
)
from api.audit.chain import record_platform_audit
from api.audit.repository import record_audit
from api.auth.dependencies import get_platform_admin_actor, require_roles, resolve_tenant_scope
from api.orchestrator.config_repository import (
    get_orchestrator_config,
    upsert_orchestrator_config,
)

router = APIRouter(prefix="/admin/outreach", tags=["outreach"])
tenant_scoped_router = APIRouter(prefix="/admin/tenants/{tenant_id}/outreach", tags=["outreach"])

_ADMIN = require_roles(Role.CLIENT_ADMIN)
_STAFF = require_roles(Role.CLIENT_ADMIN, Role.CLIENT_AGENT)
_PLATFORM = resolve_tenant_scope(Role.CLIENT_ADMIN)


class SettingsBody(BaseModel):
    enabled_channels: list[Literal["email", "call"]] = Field(default_factory=list, max_length=2)
    email_mode: Literal["auto_send", "draft_for_review"] = "draft_for_review"
    region_profile: str = "US"
    call_window_start: time = time(9, 0)
    call_window_end: time = time(20, 0)
    max_touches_per_contact: int = Field(default=3, ge=1, le=10)
    max_call_attempts: int = Field(default=2, ge=1, le=5)
    min_hours_between_touches: int = Field(default=48, ge=1, le=720)
    first_inbound_email_delay_minutes: int = Field(default=5, ge=0, le=1440)
    monthly_cost_cap_micros: int | None = Field(default=None, ge=0)
    physical_address: str | None = Field(default=None, max_length=500)
    inbound_auto_enabled: bool = False
    email_window_start: time = time(8, 0)  # A12 F4
    email_window_end: time = time(18, 0)
    # The chatbot asks for name, email and number. Stored with the orchestrator config it
    # drives; ``None`` (omitted) leaves it as it is.
    identity_gate_enabled: bool | None = None

    @model_validator(mode="after")
    def _check(self) -> SettingsBody:
        for start, end, what in ((self.call_window_start, self.call_window_end, "call"),
                                 (self.email_window_start, self.email_window_end, "email")):
            if start.tzinfo or end.tzinfo:
                raise ValueError(f"{what} window times are contact-local; omit the timezone.")
            if start >= end:
                raise ValueError(f"{what}_window_start must be before {what}_window_end.")
        return self


def _settings_out(s: AgentSettings, *, identity_gate_enabled: bool) -> dict[str, Any]:
    return {
        "identity_gate_enabled": identity_gate_enabled,
        "enabled_channels": list(s.enabled_channels),
        "email_mode": s.email_mode,
        "outreach_paused": s.outreach_paused,
        "region_profile": s.region_profile,
        "call_window_start": s.call_window_start.strftime("%H:%M"),
        "call_window_end": s.call_window_end.strftime("%H:%M"),
        "max_touches_per_contact": s.max_touches_per_contact,
        "max_call_attempts": s.max_call_attempts,
        "min_hours_between_touches": s.min_hours_between_touches,
        "first_inbound_email_delay_minutes": s.first_inbound_email_delay_minutes,
        "monthly_cost_cap_micros": s.monthly_cost_cap_micros,
        "physical_address": s.physical_address,
        "inbound_auto_enabled": s.inbound_auto_enabled,
        "email_window_start": s.email_window_start.strftime("%H:%M"),
        "email_window_end": s.email_window_end.strftime("%H:%M"),
    }


async def _require_contact(request: Request, claims: AuthClaims, contact_id: str) -> None:
    if not await contact_visible(request.app.state.db, claims, contact_id):
        raise NotFoundError("Contact not found.", code="CONTACT_NOT_FOUND")


# -- Handlers (shared by both routers) -----------------------------------------


async def _get_settings(request: Request, claims: AuthClaims) -> dict[str, Any]:
    db = request.app.state.db
    chat = await get_orchestrator_config(db, claims)
    return _settings_out(await get_agent_settings(db, claims),
                         identity_gate_enabled=chat.identity_gate_enabled)


async def _put_settings(request: Request, claims: AuthClaims, body: SettingsBody) -> dict[str, Any]:
    if body.region_profile != "US":
        raise ValidationError("Only the US region profile is supported.",
                              code="REGION_UNSUPPORTED")
    db = request.app.state.db
    current = await get_agent_settings(db, claims)
    if "email" in body.enabled_channels:
        await _require_email_ready(db, claims, body.physical_address)
    if "call" in body.enabled_channels:
        from api.call_agent.service import require_ready as require_call_ready  # noqa: PLC0415

        await require_call_ready(db, claims)  # A6: CALL_NOT_READY without a call config
    new = AgentSettings(
        **{**body.model_dump(exclude={"identity_gate_enabled"}),
           "enabled_channels": tuple(sorted(set(body.enabled_channels))),
           "outreach_paused": current.outreach_paused})
    await upsert_agent_settings(db, claims, new)
    chat = await get_orchestrator_config(db, claims)
    gate = chat.identity_gate_enabled
    if body.identity_gate_enabled is not None and body.identity_gate_enabled != gate:
        gate = body.identity_gate_enabled
        await upsert_orchestrator_config(  # a full-row write: keep everything else
            db, claims, answer_threshold=chat.answer_threshold,
            escalate_threshold=chat.escalate_threshold, turn_cap=chat.turn_cap,
            identity_gate_enabled=gate,
            low_confidence_streak_cap=chat.low_confidence_streak_cap)
    out = _settings_out(new, identity_gate_enabled=gate)
    await record_audit(db, claims, action="outreach.settings_updated", target_type="agent_settings",
                       target_id=claims.tenant_id, metadata=out,
                       actor_context=get_platform_admin_actor(request))
    return out


async def _require_email_ready(db: Any, claims: AuthClaims, address: str | None) -> None:
    """A2 E5: email can't be enabled without a sender, an address and link signing."""
    from api.email_agent.service import require_ready  # noqa: PLC0415 -- agents <- email_agent

    await require_ready(db, claims, physical_address=address)


async def _set_paused(request: Request, claims: AuthClaims, *, paused: bool) -> dict[str, Any]:
    db = request.app.state.db
    await set_tenant_paused(db, claims, paused=paused)
    await record_audit(db, claims, action="outreach.paused" if paused else "outreach.resumed",
                       target_type="agent_settings", target_id=claims.tenant_id,
                       actor_context=get_platform_admin_actor(request))
    return {"outreach_paused": paused}


class SuppressionBody(BaseModel):
    channel: Literal["email", "call", "sms", "all"]
    value: str = Field(min_length=3, max_length=320)
    reason: Literal["unsubscribe", "stop", "dnc_request", "bounce", "complaint", "wrong_number",
                    "manual", "registry"] = "manual"


def _suppression_out(s: Any) -> dict[str, Any]:
    return {"suppression_id": s.suppression_id, "channel": s.channel,
            "value": s.value_normalised, "reason": s.reason, "source": s.source,
            "created_by": s.created_by, "created_at": s.created_at.isoformat()}


async def _add_suppression(request: Request, response: Response, claims: AuthClaims,
                           body: SuppressionBody) -> dict[str, Any]:
    db = request.app.state.db
    entry, created = await add_suppression(db, claims, channel=body.channel, value=body.value,
                                           reason=body.reason, source="admin")
    if created:
        response.status_code = 201
        await record_audit(db, claims, action="outreach.suppression_added",
                           target_type="suppression", target_id=entry.suppression_id,
                           metadata={"channel": body.channel, "reason": body.reason},
                           actor_context=get_platform_admin_actor(request))
    return _suppression_out(entry)


async def _delete_suppression(request: Request, claims: AuthClaims, suppression_id: str) -> None:
    db = request.app.state.db
    if not await delete_suppression(db, claims, suppression_id):
        raise NotFoundError("Suppression not found.", code="SUPPRESSION_NOT_FOUND")
    await record_audit(db, claims, action="outreach.suppression_removed",
                       target_type="suppression", target_id=suppression_id,
                       actor_context=get_platform_admin_actor(request))


class ConsentBody(BaseModel):
    channel: Literal["email", "call", "sms"]
    status: Literal["granted", "revoked"]
    basis: Literal["express_consent", "express_written_consent", "documented_lawful_basis"]
    source: Literal["admin_form"] = "admin_form"
    wording: str | None = Field(default=None, max_length=2000)
    wording_version: str | None = Field(default=None, max_length=100)
    evidence: dict[str, Any] = Field(default_factory=dict)


def _consent_out(c: Any) -> dict[str, Any]:
    return {"consent_id": c.consent_id, "channel": c.channel, "status": c.status,
            "basis": c.basis, "source": c.source, "wording": c.wording,
            "wording_version": c.wording_version, "evidence": c.evidence,
            "recorded_by": c.recorded_by, "recorded_at": c.recorded_at.isoformat()}


async def _add_consent(request: Request, claims: AuthClaims, contact_id: str,
                       body: ConsentBody) -> dict[str, str]:
    await _require_contact(request, claims, contact_id)
    db = request.app.state.db
    consent_id = await add_consent(db, claims, contact_id, **body.model_dump())
    await record_audit(db, claims, action="outreach.consent_recorded", target_type="contact",
                       target_id=contact_id,
                       metadata={"channel": body.channel, "status": body.status,
                                 "basis": body.basis, "consent_id": consent_id},
                       actor_context=get_platform_admin_actor(request))
    return {"consent_id": consent_id}


async def _list_consents(request: Request, claims: AuthClaims,
                         contact_id: str) -> list[dict[str, Any]]:
    await _require_contact(request, claims, contact_id)
    return [_consent_out(c) for c in await list_consents(request.app.state.db, claims, contact_id)]


async def _gate(request: Request, claims: AuthClaims, contact_id: str,
                channel: Literal["email", "call"]) -> dict[str, Any]:
    await _require_contact(request, claims, contact_id)
    db = request.app.state.db
    run_id = await start_run(db, claims, agent="orchestrator")
    result = await compliance.check(db, claims, contact_id=contact_id, channel=channel,
                                    now=datetime.now(UTC), run_id=run_id)
    await finish_run(db, claims, run_id, ok=True)
    return {"ok": result.ok, "code": result.code, "detail": result.detail, "run_id": run_id}


class PlanBody(BaseModel):
    kind: PlanKind = "outbound_manual"
    goal: str | None = Field(default=None, max_length=200)


async def _request_plan(request: Request, response: Response, claims: AuthClaims,
                        contact_id: str, body: PlanBody) -> dict[str, str]:
    await _require_contact(request, claims, contact_id)
    db = request.app.state.db
    if await get_active_plan(db, claims, contact_id) is not None:
        raise ConflictError("This contact already has an active outreach plan.",
                            code="PLAN_ALREADY_ACTIVE")
    event_id = await emit(db, claims, "outreach_requested",
                          {"contact_id": contact_id, "kind": body.kind, "goal": body.goal})
    response.status_code = 202
    return {"event_id": event_id}


def _task_out(t: Any) -> dict[str, Any]:
    return {"task_id": t.task_id, "step_index": t.step_index, "channel": t.channel,
            "goal": t.goal, "intent": t.intent, "scheduled_at": t.scheduled_at.isoformat(),
            "status": t.status, "attempts": t.attempts, "max_attempts": t.max_attempts,
            "last_error": t.last_error}


async def _get_plan(request: Request, claims: AuthClaims, contact_id: str) -> dict[str, Any]:
    await _require_contact(request, claims, contact_id)
    plan = await get_active_plan(request.app.state.db, claims, contact_id)
    if plan is None:
        raise NotFoundError("No active outreach plan.", code="PLAN_NOT_FOUND")
    return {"plan_id": plan["plan_id"], "campaign_id": plan["campaign_id"],
            "status": plan["status"], "plan": plan["plan"], "policy_notes": plan["policy_notes"],
            "created_at": plan["created_at"].isoformat(),
            "tasks": [_task_out(t) for t in plan["tasks"]]}


async def _cancel_plan(request: Request, claims: AuthClaims, plan_id: str) -> dict[str, Any]:
    db = request.app.state.db
    contact_id = await get_plan_contact_id(db, claims, plan_id)
    if contact_id is None or not await contact_visible(db, claims, contact_id):
        raise NotFoundError("Plan not found.", code="PLAN_NOT_FOUND")
    cancelled = await stop_plan(db, claims, plan_id, reason="cancelled_by_user")
    await record_audit(db, claims, action="outreach.plan_cancelled", target_type="outreach_plan",
                       target_id=plan_id, metadata={"tasks_cancelled": cancelled},
                       actor_context=get_platform_admin_actor(request))
    return {"plan_id": plan_id, "tasks_cancelled": cancelled}


async def _event_status(request: Request, claims: AuthClaims, event_id: str) -> dict[str, Any]:
    row = await get_event_status(request.app.state.db, claims, event_id)
    if row is None:
        raise NotFoundError("Event not found.", code="EVENT_NOT_FOUND")
    return {**row, "created_at": row["created_at"].isoformat(),
            "processed_at": row["processed_at"].isoformat() if row["processed_at"] else None}


# -- Route wiring ----------------------------------------------------------------


def _mount(r: APIRouter, admin: Callable[..., Any], staff: Callable[..., Any]) -> None:
    """Register every tenant route on ``r`` with the given role dependencies."""

    @r.get("/settings")
    async def get_settings(request: Request,
                           claims: AuthClaims = Depends(staff)) -> dict[str, Any]:  # noqa: B008
        return await _get_settings(request, claims)

    @r.put("/settings")
    async def put_settings(body: SettingsBody, request: Request,
                           claims: AuthClaims = Depends(admin)) -> dict[str, Any]:  # noqa: B008
        return await _put_settings(request, claims, body)

    @r.post("/pause")
    async def pause(request: Request,
                    claims: AuthClaims = Depends(admin)) -> dict[str, Any]:  # noqa: B008
        return await _set_paused(request, claims, paused=True)

    @r.post("/resume")
    async def resume(request: Request,
                     claims: AuthClaims = Depends(admin)) -> dict[str, Any]:  # noqa: B008
        return await _set_paused(request, claims, paused=False)

    @r.get("/suppressions")
    async def suppressions(
        request: Request,
        channel: Literal["email", "call", "sms", "all"] | None = None,
        q: str | None = Query(default=None, max_length=320),
        limit: int = 50,
        offset: int = 0,
        claims: AuthClaims = Depends(staff),  # noqa: B008
    ) -> list[dict[str, Any]]:
        rows = await list_suppressions(request.app.state.db, claims, channel=channel, q=q,
                                       limit=limit, offset=offset)
        return [_suppression_out(s) for s in rows]

    @r.post("/suppressions")
    async def add_supp(body: SuppressionBody, request: Request, response: Response,
                       claims: AuthClaims = Depends(staff)) -> dict[str, Any]:  # noqa: B008
        return await _add_suppression(request, response, claims, body)

    @r.delete("/suppressions/{suppression_id}", status_code=204)
    async def del_supp(suppression_id: str, request: Request,
                       claims: AuthClaims = Depends(admin)) -> None:  # noqa: B008
        await _delete_suppression(request, claims, suppression_id)

    @r.post("/contacts/{contact_id}/consents", status_code=201)
    async def post_consent(contact_id: str, body: ConsentBody, request: Request,
                           claims: AuthClaims = Depends(staff)) -> dict[str, str]:  # noqa: B008
        return await _add_consent(request, claims, contact_id, body)

    @r.get("/contacts/{contact_id}/consents")
    async def get_consents(contact_id: str, request: Request,
                           claims: AuthClaims = Depends(staff)) -> list[dict[str, Any]]:  # noqa: B008
        return await _list_consents(request, claims, contact_id)

    @r.get("/contacts/{contact_id}/gate")
    async def gate(contact_id: str, request: Request, channel: Literal["email", "call"],
                   claims: AuthClaims = Depends(staff)) -> dict[str, Any]:  # noqa: B008
        return await _gate(request, claims, contact_id, channel)

    @r.post("/contacts/{contact_id}/plans")
    async def post_plan(contact_id: str, body: PlanBody, request: Request, response: Response,
                        claims: AuthClaims = Depends(staff)) -> dict[str, str]:  # noqa: B008
        return await _request_plan(request, response, claims, contact_id, body)

    @r.get("/contacts/{contact_id}/plan")
    async def get_plan(contact_id: str, request: Request,
                       claims: AuthClaims = Depends(staff)) -> dict[str, Any]:  # noqa: B008
        return await _get_plan(request, claims, contact_id)

    @r.post("/plans/{plan_id}:cancel")
    async def cancel_plan(plan_id: str, request: Request,
                          claims: AuthClaims = Depends(staff)) -> dict[str, Any]:  # noqa: B008
        return await _cancel_plan(request, claims, plan_id)

    @r.get("/events/{event_id}")
    async def event_status(event_id: str, request: Request,
                           claims: AuthClaims = Depends(staff)) -> dict[str, Any]:  # noqa: B008
        return await _event_status(request, claims, event_id)


_mount(router, _ADMIN, _STAFF)
_mount(tenant_scoped_router, _PLATFORM, _PLATFORM)


class PlatformPauseBody(BaseModel):
    paused: bool


@router.put("/platform/pause")
async def platform_pause(
    body: PlatformPauseBody,
    request: Request,
    claims: AuthClaims = Depends(require_roles(Role.PLATFORM_ADMIN)),  # noqa: B008
) -> dict[str, bool]:
    """The global kill switch. PLATFORM_ADMIN only; audited under ``__platform__``."""
    db = request.app.state.db
    await set_platform_paused(db, claims, paused=body.paused)
    await record_platform_audit(
        db, claims, action="outreach.platform_paused" if body.paused else
        "outreach.platform_resumed", target_type="platform_flags", target_id="outreach_paused")
    return {"outreach_paused": body.paused}

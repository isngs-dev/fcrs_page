"""Console reads for the Outreach area (A8): overview, campaigns, a lead's agent
activity, human takeover.

Same mounting as the other outreach routes: implicit ``/admin/outreach`` for
CLIENT_ADMIN/CLIENT_AGENT, ``/admin/tenants/{tenant_id}/outreach`` for
PLATFORM_ADMIN. Agents only ever reach contacts they can see (404 otherwise).
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from common.auth import AuthClaims, Role
from common.errors import ConflictError, NotFoundError, ValidationError
from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, field_validator

from api.agents import memory, reporting
from api.agents.data_rights import erase_agent_data, export_agent_data
from api.agents.events import emit
from api.agents.outreach_repository import (
    get_latest_plan,
    list_campaigns,
    plan_counts,
    set_campaign_end,
    stop_campaign,
    stop_contact_plan,
)
from api.agents.repository import (
    contact_visible,
    get_agent_settings,
    get_platform_paused,
    list_consents,
    month_costs_by_kind,
)
from api.agents.routes import _consent_out, _task_out
from api.audit.repository import record_audit
from api.auth.dependencies import get_platform_admin_actor, require_roles, resolve_tenant_scope
from api.call_agent import repository as call_repo
from api.contacts.repository import get_contact_by_lead_id
from api.email_agent import repository as email_repo
from api.email_agent.routes import _message_out
from api.leads.repository import get_lead

router = APIRouter(prefix="/admin/outreach", tags=["outreach"])
tenant_scoped_router = APIRouter(prefix="/admin/tenants/{tenant_id}/outreach", tags=["outreach"])


class MeetingOutcome(BaseModel):
    outcome: Literal["attended", "no_show"]


class CampaignPatch(BaseModel):
    ends_at: datetime | None = None  # A12 F5: no new outreach after this instant

    @field_validator("ends_at")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("ends_at needs a timezone (e.g. 2026-12-31T23:59:00Z).")
        return value


def _iso(row: dict[str, Any]) -> dict[str, Any]:
    return {k: v.isoformat() if hasattr(v, "isoformat") else v for k, v in row.items()}


async def _overview(request: Request, claims: AuthClaims) -> dict[str, Any]:
    db = request.app.state.db
    now = datetime.now(UTC)
    week = now - timedelta(days=7)
    settings = await get_agent_settings(db, claims)
    emails = await email_repo.counts_since(db, claims, week)
    calls = await call_repo.counts_since(db, claims, week)
    costs = None
    if claims.role is not Role.CLIENT_AGENT:  # spend is an admin concern (U3)
        by_kind = await month_costs_by_kind(db, claims, now=now)
        costs = {"month_micros": sum(by_kind.values()),
                 "cap_micros": settings.monthly_cost_cap_micros, "by_kind": by_kind}
    return {
        "outreach_paused": settings.outreach_paused,
        "platform_paused": await get_platform_paused(db),
        "enabled_channels": list(settings.enabled_channels),
        "email_mode": settings.email_mode,
        **await plan_counts(db, claims, now=now),
        "drafts_awaiting": emails["drafts"], "emails_sent_7d": emails["sent"],
        "calls_7d": calls["calls"], "calls_booked_7d": calls["booked"],
        "costs": costs,
    }


async def _stop_campaign(request: Request, claims: AuthClaims, campaign_id: str
                         ) -> dict[str, Any]:
    db = request.app.state.db
    stopped = await stop_campaign(db, claims, campaign_id)
    if stopped is None:
        raise NotFoundError("Campaign not found.", code="CAMPAIGN_NOT_FOUND")
    await record_audit(db, claims, action="outreach.campaign_stopped",
                       target_type="outreach_campaign", target_id=campaign_id,
                       metadata={"plans_stopped": stopped},
                       actor_context=get_platform_admin_actor(request))
    return {"campaign_id": campaign_id, "status": "cancelled", "plans_stopped": stopped}


async def _lead_activity(request: Request, claims: AuthClaims, lead_id: str) -> dict[str, Any]:
    db = request.app.state.db
    lead = await get_lead(db, claims, lead_id)
    if lead is None:
        raise NotFoundError("Lead not found.", code="LEAD_NOT_FOUND")
    contact = await get_contact_by_lead_id(db, claims, lead_id)
    if contact is None:
        if claims.role is Role.CLIENT_AGENT and lead.assigned_agent_id != claims.subject:
            raise NotFoundError("Lead not found.", code="LEAD_NOT_FOUND")
        return {"contact_id": None, "plan": None, "consents": [], "emails": [], "calls": [],
                "memory": []}
    cid = contact.contact_id
    if not await contact_visible(db, claims, cid):
        raise NotFoundError("Lead not found.", code="LEAD_NOT_FOUND")
    plan = await get_latest_plan(db, claims, cid)
    emails = await email_repo.list_messages(db, claims, status=None, limit=50, offset=0,
                                            contact_id=cid)
    calls = await call_repo.list_sessions(db, claims, status=None, limit=50, offset=0,
                                          contact_id=cid)
    return {
        "contact_id": cid,
        "plan": None if plan is None else {
            "plan_id": plan["plan_id"], "campaign_id": plan["campaign_id"],
            "status": plan["status"], "stop_reason": plan["stop_reason"],
            "created_at": plan["created_at"].isoformat(),
            "tasks": [_task_out(t) for t in plan["tasks"]]},
        "consents": [_consent_out(c) for c in await list_consents(db, claims, cid)],
        "emails": [_message_out(m) for m in emails],
        "calls": [_iso(c) for c in calls],
        "memory": await memory.items(db, claims, cid),  # A11: what Rebecca knows
    }


async def _takeover(request: Request, claims: AuthClaims, contact_id: str) -> dict[str, Any]:
    db = request.app.state.db
    if not await contact_visible(db, claims, contact_id):
        raise NotFoundError("Contact not found.", code="CONTACT_NOT_FOUND")
    # Stop now so the person sees it at once; the event still goes out for any other
    # listener (its own stop handler then finds nothing active -- idempotent).
    cancelled = await stop_contact_plan(db, claims, contact_id, reason="human_takeover")
    event_id = await emit(db, claims, "human_takeover", {"contact_id": contact_id})
    await record_audit(db, claims, action="outreach.human_takeover", target_type="contact",
                       target_id=contact_id, metadata={"tasks_cancelled": cancelled},
                       actor_context=get_platform_admin_actor(request))
    return {"event_id": event_id, "tasks_cancelled": cancelled}


async def _agent_data(request: Request, claims: AuthClaims, contact_id: str, *,
                      erase: bool) -> dict[str, Any]:
    """A9 H9 (compliance §6): export or erase a contact's agent data. Audited."""
    db = request.app.state.db
    result = await (erase_agent_data if erase else export_agent_data)(db, claims, contact_id)
    await record_audit(db, claims,
                       action="outreach.agent_data_erased" if erase
                       else "outreach.agent_data_exported",
                       target_type="contact", target_id=contact_id,
                       metadata={k: v for k, v in result.items() if isinstance(v, int)},
                       actor_context=get_platform_admin_actor(request))
    return result


async def _meeting_outcome(request: Request, claims: AuthClaims, event_id: str,
                           outcome: str) -> dict[str, str]:
    """A13 R3: admins, or the agent the meeting's lead is assigned to."""
    db = request.app.state.db
    meeting = await reporting.meeting_for_outcome(db, claims, event_id)
    visible = meeting is not None and (claims.role is not Role.CLIENT_AGENT or (
        meeting["lead_id"] is not None and (lead := await get_lead(db, claims, meeting["lead_id"]))
        is not None and lead.assigned_agent_id == claims.subject))
    if not visible:
        raise NotFoundError("Meeting not found.", code="MEETING_NOT_FOUND")
    status = "completed" if outcome == "attended" else "no_show"
    if not await reporting.set_meeting_outcome(db, claims, event_id, status):
        raise ConflictError("Only a past meeting can be marked.", code="MEETING_NOT_PAST")
    await record_audit(db, claims, action="outreach.meeting_outcome", target_type="schedule_event",
                       target_id=event_id, metadata={"outcome": outcome},
                       actor_context=get_platform_admin_actor(request))
    return {"event_id": event_id, "status": status}


def _mount(r: APIRouter, admin: Callable[..., Any], staff: Callable[..., Any]) -> None:

    @r.get("/statuses")
    async def statuses(request: Request, status: str | None = None, limit: int = 50,
                       offset: int = 0,
                       claims: AuthClaims = Depends(admin)) -> dict[str, Any]:  # noqa: B008
        if status is not None and status not in reporting.STATUSES:
            raise ValidationError("Unknown status.", code="STATUS_UNKNOWN")
        db = request.app.state.db
        return {"counts": await reporting.status_counts(db, claims),
                "contacts": await reporting.list_statuses(db, claims, status=status,
                                                          limit=limit, offset=offset)}

    @r.get("/performance")
    async def performance(request: Request, days: int = Query(default=30, ge=1, le=365),
                          claims: AuthClaims = Depends(staff)) -> dict[str, Any]:  # noqa: B008
        db = request.app.state.db
        out = await reporting.performance(db, claims, now=datetime.now(UTC), days=days,
                                          with_costs=claims.role is not Role.CLIENT_AGENT)
        out["meetings_to_mark"] = [_iso(m) for m in await reporting.meetings_to_mark(db, claims)]
        return out

    @r.post("/meetings/{event_id}:outcome")
    async def meeting_outcome(event_id: str, body: MeetingOutcome, request: Request,
                              claims: AuthClaims = Depends(staff)) -> dict[str, str]:  # noqa: B008
        return await _meeting_outcome(request, claims, event_id, body.outcome)

    @r.get("/exceptions")
    async def exceptions(request: Request,
                         claims: AuthClaims = Depends(admin)) -> dict[str, Any]:  # noqa: B008
        found = await reporting.exceptions(request.app.state.db, claims, now=datetime.now(UTC))
        found["tasks"] = [_iso(t) for t in found["tasks"]]
        found["dead_events"] = [_iso(e) for e in found["dead_events"]]
        return found

    @r.post("/tasks/{task_id}:retry")
    async def retry_task(task_id: str, request: Request,
                         claims: AuthClaims = Depends(admin)) -> dict[str, str]:  # noqa: B008
        db = request.app.state.db
        result = await reporting.retry_task(db, claims, task_id)
        if result is None:
            raise NotFoundError("Nothing to retry.", code="TASK_NOT_FOUND")
        if result == "PLAN_NOT_ACTIVE":
            raise ConflictError("Its follow-up plan has ended; start a new one instead.",
                                code="PLAN_NOT_ACTIVE")
        await record_audit(db, claims, action="outreach.task_retried", target_type="outreach_task",
                           target_id=task_id, actor_context=get_platform_admin_actor(request))
        return {"task_id": task_id, "status": "scheduled"}

    @r.post("/tasks/{task_id}:dismiss")
    async def dismiss_task(task_id: str, request: Request,
                           claims: AuthClaims = Depends(admin)) -> dict[str, str]:  # noqa: B008
        db = request.app.state.db
        if not await reporting.dismiss_task(db, claims, task_id):
            raise NotFoundError("Nothing to dismiss.", code="TASK_NOT_FOUND")
        await record_audit(db, claims, action="outreach.task_dismissed",
                           target_type="outreach_task", target_id=task_id,
                           actor_context=get_platform_admin_actor(request))
        return {"task_id": task_id, "status": "cancelled"}

    @r.post("/events/{event_id}:retry")
    async def retry_event(event_id: str, request: Request,
                          claims: AuthClaims = Depends(admin)) -> dict[str, str]:  # noqa: B008
        db = request.app.state.db
        if not await reporting.retry_event(db, claims, event_id):
            raise NotFoundError("Nothing to retry.", code="EVENT_NOT_FOUND")
        await record_audit(db, claims, action="outreach.event_retried", target_type="agent_event",
                           target_id=event_id, actor_context=get_platform_admin_actor(request))
        return {"event_id": event_id, "status": "pending"}

    @r.get("/overview")
    async def overview(request: Request,
                       claims: AuthClaims = Depends(staff)) -> dict[str, Any]:  # noqa: B008
        return await _overview(request, claims)

    @r.get("/campaigns")
    async def campaigns(request: Request, limit: int = 50, offset: int = 0,
                        claims: AuthClaims = Depends(admin)) -> list[dict[str, Any]]:  # noqa: B008
        rows = await list_campaigns(request.app.state.db, claims, limit=limit, offset=offset)
        return [_iso(r) for r in rows]

    @r.patch("/campaigns/{campaign_id}")
    async def set_end(campaign_id: str, body: CampaignPatch, request: Request,
                      claims: AuthClaims = Depends(admin)) -> dict[str, Any]:  # noqa: B008
        db = request.app.state.db
        if not await set_campaign_end(db, claims, campaign_id, body.ends_at):
            raise NotFoundError("Campaign not found.", code="CAMPAIGN_NOT_FOUND")
        await record_audit(db, claims, action="outreach.campaign_end_set",
                           target_type="outreach_campaign", target_id=campaign_id,
                           metadata={"ends_at": body.ends_at.isoformat() if body.ends_at
                                     else None},
                           actor_context=get_platform_admin_actor(request))
        return {"campaign_id": campaign_id,
                "ends_at": body.ends_at.isoformat() if body.ends_at else None}

    @r.post("/campaigns/{campaign_id}:stop")
    async def stop(campaign_id: str, request: Request,
                   claims: AuthClaims = Depends(admin)) -> dict[str, Any]:  # noqa: B008
        return await _stop_campaign(request, claims, campaign_id)

    @r.get("/leads/{lead_id}/activity")
    async def lead_activity(lead_id: str, request: Request,
                            claims: AuthClaims = Depends(staff)) -> dict[str, Any]:  # noqa: B008
        return await _lead_activity(request, claims, lead_id)

    @r.get("/contacts/{contact_id}/agent-data")
    async def export_data(contact_id: str, request: Request,
                          claims: AuthClaims = Depends(admin)) -> dict[str, Any]:  # noqa: B008
        return await _agent_data(request, claims, contact_id, erase=False)

    @r.delete("/contacts/{contact_id}/agent-data")
    async def erase_data(contact_id: str, request: Request,
                         claims: AuthClaims = Depends(admin)) -> dict[str, Any]:  # noqa: B008
        return await _agent_data(request, claims, contact_id, erase=True)

    @r.post("/contacts/{contact_id}:takeover")
    async def takeover(contact_id: str, request: Request,
                       claims: AuthClaims = Depends(staff)) -> dict[str, Any]:  # noqa: B008
        return await _takeover(request, claims, contact_id)


_mount(router, require_roles(Role.CLIENT_ADMIN), require_roles(Role.CLIENT_ADMIN,
                                                                Role.CLIENT_AGENT))
_platform = resolve_tenant_scope(Role.CLIENT_ADMIN)
_mount(tenant_scoped_router, _platform, _platform)

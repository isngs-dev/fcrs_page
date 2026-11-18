"""The public "manage your booking" page (plan 1.6, owner 2026-11-18).

The signed token is the only credential: tenant and booking come ONLY from it (forged, or
a booking not in that tenant -> 404). GET shows the booking, the next open times and a
cancel button and changes nothing (mail scanners follow links). Moving books the new time
FIRST, then retires the old booking -- a failed new booking leaves the old one untouched.
"""
from __future__ import annotations

import html
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from common.auth import AuthClaims, Role
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from api.audit.repository import record_audit
from api.notifications.emit import emit_event_safe
from api.scheduling import booking, manage
from api.scheduling.repository import ScheduleEvent, get_event
from api.tenants.repository import TenantRepository

router = APIRouter(prefix="/public/bookings", tags=["scheduling"])

_SLOTS_SHOWN = 12
_PAGE = (
    "<!doctype html><html><head><meta charset='utf-8'>"
    "<meta name='viewport' content='width=device-width,initial-scale=1'>"
    "<title>{title}</title></head><body style='font-family:Arial,Helvetica,sans-serif;"
    "max-width:520px;margin:40px auto;padding:0 16px;color:#1e293b;line-height:1.5'>"
    "{body}</body></html>"
)
_BUTTON = "padding:10px 18px;font-size:15px;border-radius:8px;border:1px solid #cbd5e1;"


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(_PAGE.format(title=html.escape(title), body=body), status_code=status)


_NOT_FOUND = ("Link not valid", "<p>This booking link isn't valid.</p>", 404)


def _when(at: datetime, timezone: str) -> str:
    local = at.astimezone(ZoneInfo(timezone))
    return f"{local:%A, %B} {local.day} at {local:%I:%M %p %Z}".replace(" 0", " ")


def _claims(tenant_id: str) -> AuthClaims:
    return AuthClaims(subject="system:booking-link", role=Role.CLIENT_ADMIN, tenant_id=tenant_id)


async def _resolve(request: Request, token: str) -> tuple[AuthClaims, ScheduleEvent] | None:
    target = manage.verify(token)
    if target is None:
        return None
    claims = _claims(target[0])
    event = await get_event(request.app.state.db, claims, target[1])
    return (claims, event) if event is not None else None


async def _business(request: Request, claims: AuthClaims) -> str | None:
    tenant = await TenantRepository(request.app.state.db).get(claims, str(claims.tenant_id))
    return tenant.get("name") if tenant is not None else None


def _inactive(event: ScheduleEvent) -> HTMLResponse | None:
    if event.status != "booked" or event.starts_at <= datetime.now(UTC):
        return _page("Booking", "<p>This call is no longer booked. If you'd like a new time, "
                                "just reply to our email.</p>")
    return None


@router.get("/{token}")
async def manage_page(token: str, request: Request) -> HTMLResponse:
    found = await _resolve(request, token)
    if found is None:
        return _page(*_NOT_FOUND)
    claims, event = found
    if (closed := _inactive(event)) is not None:
        return closed
    business = await _business(request, claims)
    open_slots = await booking.next_open_slots(request.app.state.db, claims, datetime.now(UTC),
                                               count=_SLOTS_SHOWN)
    with_business = f" with {html.escape(business)}" if business else ""
    parts = [f"<h2>Your call{with_business}</h2>",
             f"<p><b>{html.escape(_when(event.starts_at, event.timezone))}</b></p>"]
    if event.meet_url:
        parts.append(f"<p>Join the call: <a href='{html.escape(event.meet_url)}'>"
                     f"{html.escape(event.meet_url)}</a></p>")
    if open_slots and open_slots[0]:
        choices = "".join(
            f"<label style='display:block;margin:6px 0'><input type='radio' name='starts_at' "
            f"value='{html.escape(at.isoformat())}' required> "
            f"{html.escape(_when(at, event.timezone))}</label>" for at in open_slots[0])
        parts.append(f"<h3>Move it to another time</h3><form method='post' action='{token}/move'>"
                     f"{choices}<button type='submit' style='{_BUTTON}margin-top:8px'>"
                     "Move my call</button></form>")
    parts.append(f"<h3>Can't make it?</h3><form method='post' action='{token}/cancel'>"
                 f"<button type='submit' style='{_BUTTON}'>Cancel my call</button></form>")
    return _page("Your booking", "".join(parts))


@router.post("/{token}/move")
async def move_booking(token: str, request: Request,
                       starts_at: str = Form(default="")) -> HTMLResponse:
    found = await _resolve(request, token)
    if found is None:
        return _page(*_NOT_FOUND)
    claims, event = found
    if (closed := _inactive(event)) is not None:
        return closed
    db = request.app.state.db
    taken = _page("Booking", "<p>Sorry, that time isn't open any more. "
                             f"<a href='../{token}'>Pick another time</a>.</p>")
    try:
        start = datetime.fromisoformat(starts_at).astimezone(UTC)
    except ValueError:
        return taken
    status, new, meet_url, organizer = await booking.book_slot(
        db, claims, start=start, timezone=event.timezone, email=event.email, name=event.name,
        lead_id=event.lead_id, visitor_id=event.visitor_id, attendee_phone=None,
        consent={"granted": True, "purpose": "booking", "moved_from": event.event_id,
                 "text": "Moved by the attendee from their booking link"})
    if new is None:
        return taken if status == "taken" else _page(
            "Booking", "<p>We couldn't move your call just now. Please try again in a "
                       "minute.</p>", 503)
    await booking.retire_booking(db, claims, event.event_id)  # only after the new one stands
    business = await _business(request, claims)
    if event.email:
        await booking.send_confirmation(db, claims, new, recipient=event.email, name=event.name,
                                        business=business, meet_url=meet_url,
                                        organizer=organizer, rescheduled_from=event.starts_at)
    if organizer:
        await booking.notify_organizer(db, claims, new, organizer, name=event.name,
                                       email=event.email, phone=None, meet_url=meet_url,
                                       rescheduled_from=event.starts_at)
    await record_audit(db, claims, action="scheduling.booking_moved",
                       target_type="schedule_event", target_id=new.event_id,
                       metadata={"from_event_id": event.event_id, "via": "booking_link"})
    await emit_event_safe(db, claims, kind="booking_moved_by_attendee", category="leads",
                          target_type="schedule_event", target_id=new.event_id,
                          payload={"from_event_id": event.event_id}, actor_id=None)
    return _page("Booking moved", f"<p>Your call is moved to <b>"
                                  f"{html.escape(_when(new.starts_at, new.timezone))}</b>. "
                                  "We've emailed you the new details.</p>")


@router.post("/{token}/cancel")
async def cancel_booking(token: str, request: Request) -> HTMLResponse:
    found = await _resolve(request, token)
    if found is None:
        return _page(*_NOT_FOUND)
    claims, event = found
    if (closed := _inactive(event)) is not None:
        return closed
    db = request.app.state.db
    if not await booking.retire_booking(db, claims, event.event_id):
        return _page("Booking", "<p>This call is no longer booked.</p>")
    await record_audit(db, claims, action="scheduling.booking_cancelled",
                       target_type="schedule_event", target_id=event.event_id,
                       metadata={"via": "booking_link"})
    await emit_event_safe(db, claims, kind="booking_cancelled_by_attendee", category="leads",
                          target_type="schedule_event", target_id=event.event_id,
                          payload={"starts_at": event.starts_at.isoformat()}, actor_id=None)
    return _page("Booking cancelled", "<p>Your call is cancelled. If you'd like a new time, "
                                      "just reply to our email.</p>")

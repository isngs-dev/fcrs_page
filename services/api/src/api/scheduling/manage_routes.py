"""The public "manage your booking" page (plan 1.6, owner 2026-11-18).

The signed token is the only credential: tenant and booking come ONLY from it (forged, or
a booking not in that tenant -> 404). GET shows the booking, the next open times and a
cancel button and changes nothing (mail scanners follow links). Moving books the new time
FIRST, then retires the old booking -- a failed new booking leaves the old one untouched.
"""
from __future__ import annotations

import base64
import hashlib
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
# One <style> block, allowed by its hash: the API's default CSP (default-src 'none') blocks
# every style, which left this page bare (owner, 2026-11-19). No scripts, no inline styles.
_CSS = (
    "*{box-sizing:border-box}"
    "body{margin:0;background:#f1f5f9;color:#0f172a;line-height:1.5;font-size:15px;"
    "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif}"
    "main{max-width:560px;margin:40px auto;padding:0 16px}"
    ".card{background:#fff;border:1px solid #e2e8f0;border-radius:16px;padding:28px;"
    "box-shadow:0 1px 3px rgba(15,23,42,.06)}"
    ".card+.card{margin-top:16px}"
    "h1{margin:0 0 16px;font-size:22px;line-height:1.3}"
    "h2{margin:0 0 4px;font-size:17px}"
    "p{margin:0 0 12px}"
    ".when{margin:0;padding:14px 16px;border-radius:12px;background:#eef2ff;color:#3730a3;"
    "font-weight:600;font-size:16px}"
    ".muted{color:#64748b;font-size:14px}"
    ".day{margin:18px 0 8px;font-size:14px;font-weight:600;color:#334155}"
    ".slots{display:grid;grid-template-columns:repeat(auto-fill,minmax(104px,1fr));gap:8px}"
    ".slot{position:relative}"
    ".slot input{position:absolute;inset:0;margin:0;opacity:0;cursor:pointer}"
    ".slot span{display:block;padding:10px 0;text-align:center;border:1px solid #cbd5e1;"
    "border-radius:10px;background:#fff}"
    ".slot input:hover+span{border-color:#4f46e5}"
    ".slot input:checked+span{background:#4f46e5;border-color:#4f46e5;color:#fff;"
    "font-weight:600}"
    ".slot input:focus-visible+span{outline:3px solid #a5b4fc;outline-offset:2px}"
    ".btn{display:inline-block;margin-top:20px;padding:12px 20px;border-radius:10px;"
    "border:1px solid transparent;font:inherit;font-weight:600;cursor:pointer;"
    "text-decoration:none}"
    ".primary{background:#4f46e5;color:#fff}"
    ".dark{background:#0f172a;color:#fff;margin-top:16px}"
    ".danger{background:#fff;color:#b91c1c;border-color:#fecaca}"
    "a{color:#4f46e5}"
)
_CSP = ("default-src 'none'; style-src 'sha256-"
        + base64.b64encode(hashlib.sha256(_CSS.encode()).digest()).decode()
        + "'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
_PAGE = (
    "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
    "<meta name='viewport' content='width=device-width,initial-scale=1'>"
    "<title>{title}</title><style>{css}</style></head><body><main>{body}</main></body></html>"
)


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(_PAGE.format(title=html.escape(title), css=_CSS, body=body),
                        status_code=status, headers={"Content-Security-Policy": _CSP})


def _message(title: str, text: str, status: int = 200) -> HTMLResponse:
    """A one-card page: a heading and a short message (``text`` is trusted HTML)."""
    return _page(title, f"<div class='card'><h1>{html.escape(title)}</h1>{text}</div>", status)


_NOT_FOUND = ("Link not valid", "<p>This booking link isn't valid.</p>", 404)


def _local(at: datetime, timezone: str) -> datetime:
    return at.astimezone(ZoneInfo(timezone))


def _day(local: datetime) -> str:
    return f"{local:%A, %B} {local.day}"


def _time(local: datetime) -> str:
    return f"{local:%I:%M %p}".lstrip("0")


def _when(at: datetime, timezone: str) -> str:
    local = _local(at, timezone)
    return f"{_day(local)} at {_time(local)} {local:%Z}"


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
        return _message("No longer booked", "<p>This call is no longer booked. If you'd like "
                                            "a new time, just reply to our email.</p>")
    return None


@router.get("/{token}")
async def manage_page(token: str, request: Request) -> HTMLResponse:
    found = await _resolve(request, token)
    if found is None:
        return _message(*_NOT_FOUND)
    claims, event = found
    if (closed := _inactive(event)) is not None:
        return closed
    business = await _business(request, claims)
    open_slots = await booking.next_open_slots(request.app.state.db, claims, datetime.now(UTC),
                                               count=_SLOTS_SHOWN)
    with_business = f" with {html.escape(business)}" if business else ""
    booked = (f"<div class='card'><h1>Your call{with_business}</h1>"
              f"<p class='when'>{html.escape(_when(event.starts_at, event.timezone))}</p>")
    if event.meet_url:
        booked += (f"<a class='btn dark' href='{html.escape(event.meet_url)}'>"
                   "Join the call</a>")
    parts = [booked + "</div>"]
    if open_slots and open_slots[0]:
        days: dict[str, list[str]] = {}
        for at in open_slots[0]:
            local = _local(at, event.timezone)
            days.setdefault(_day(local), []).append(
                f"<label class='slot'><input type='radio' name='starts_at' "
                f"value='{html.escape(at.isoformat())}' required>"
                f"<span>{_time(local)}</span></label>")
        zone = _local(open_slots[0][0], event.timezone).strftime("%Z")
        choices = "".join(f"<p class='day'>{html.escape(day)}</p><div class='slots'>"
                          f"{''.join(slots)}</div>" for day, slots in days.items())
        parts.append(f"<div class='card'><h2>Move it to another time</h2>"
                     f"<p class='muted'>Pick a new time (times in {html.escape(zone)}).</p>"
                     f"<form method='post' action='{token}/move'>{choices}"
                     "<button type='submit' class='btn primary'>Move my call</button>"
                     "</form></div>")
    parts.append("<div class='card'><h2>Can't make it?</h2>"
                 "<p class='muted'>No problem, you can cancel it here.</p>"
                 f"<form method='post' action='{token}/cancel'>"
                 "<button type='submit' class='btn danger'>Cancel my call</button></form></div>")
    return _page("Your booking", "".join(parts))


@router.post("/{token}/move")
async def move_booking(token: str, request: Request,
                       starts_at: str = Form(default="")) -> HTMLResponse:
    found = await _resolve(request, token)
    if found is None:
        return _message(*_NOT_FOUND)
    claims, event = found
    if (closed := _inactive(event)) is not None:
        return closed
    db = request.app.state.db
    taken = _message("That time was just taken", "<p>Sorry, that time isn't open any more."
                                                 f"</p><a class='btn primary' href='../{token}'>"
                                                 "Pick another time</a>")
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
        return taken if status == "taken" else _message(
            "Couldn't move your call", "<p>We couldn't move your call just now. Please try "
                                       "again in a minute.</p>", 503)
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
    return _message("Booking moved", "<p>Your call is moved to:</p><p class='when'>"
                                     f"{html.escape(_when(new.starts_at, new.timezone))}</p>"
                                     "<p class='muted'>We've emailed you the new details.</p>")


@router.post("/{token}/cancel")
async def cancel_booking(token: str, request: Request) -> HTMLResponse:
    found = await _resolve(request, token)
    if found is None:
        return _message(*_NOT_FOUND)
    claims, event = found
    if (closed := _inactive(event)) is not None:
        return closed
    db = request.app.state.db
    if not await booking.retire_booking(db, claims, event.event_id):
        return _message("No longer booked", "<p>This call is no longer booked.</p>")
    await record_audit(db, claims, action="scheduling.booking_cancelled",
                       target_type="schedule_event", target_id=event.event_id,
                       metadata={"via": "booking_link"})
    await emit_event_safe(db, claims, kind="booking_cancelled_by_attendee", category="leads",
                          target_type="schedule_event", target_id=event.event_id,
                          payload={"starts_at": event.starts_at.isoformat()}, actor_id=None)
    return _message("Booking cancelled", "<p>Your call is cancelled. If you'd like a new "
                                         "time, just reply to our email.</p>")

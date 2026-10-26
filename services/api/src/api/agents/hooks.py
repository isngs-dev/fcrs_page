"""Hooks other modules call to tell the agents something happened (A2 E10).

These run AFTER the caller's own write has committed (the booking flows don't
share a transaction with us), so a failure here is logged and swallowed --
it must never fail a booking that already happened.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from common.auth import AuthClaims
from common.db import Database
from common.logging import get_logger

from api.agents.events import emit
from api.contacts.repository import get_contact_id_by_identity

_log = get_logger(__name__)


async def meeting_booked(db: Database, claims: AuthClaims, *, email: str | None) -> None:
    """A booking by a known contact stops their outreach plan."""
    if not email:
        return
    try:
        contact_id = await get_contact_id_by_identity(db, claims, identity_type="email",
                                                      identity_value=email)
        if contact_id is not None:
            await emit(db, claims, "meeting_booked", {"contact_id": contact_id})
    except Exception as exc:  # noqa: BLE001 -- the booking already committed
        _log.error("meeting_booked hook failed", extra={
            "event": "agent_meeting_booked_hook_failed", "reason": type(exc).__name__})


async def inbound_lead_created(
    db: Database, claims: AuthClaims, *, lead_id: str, channels: list[str], version: str | None,
    timezone: str | None, form: str, consents: list[dict[str, Any]] | None = None,
    answers: dict[str, str] | None = None,
) -> None:
    """A4 H6: a widget lead ticked channel consent -> the agents take it from here.
    ``consents`` (A10): explicit evidence from a form instead of a widget wording version."""
    if not channels:
        return
    try:
        await emit(db, claims, "inbound_lead_created", {
            "lead_id": lead_id, "channels": channels, "version": version,
            "timezone": timezone, "form": form,
            "captured_at": datetime.now(UTC).isoformat(),
            **({"consents": consents} if consents else {}),
            **({"answers": dict(list(answers.items())[:10])} if answers else {})})
    except Exception as exc:  # noqa: BLE001 -- the lead already committed
        _log.error("inbound_lead_created hook failed", extra={
            "event": "agent_inbound_lead_hook_failed", "reason": type(exc).__name__})

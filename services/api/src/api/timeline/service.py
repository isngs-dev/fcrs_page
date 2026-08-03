"""Fan-out + merge for the SR-9.3 customer-360 timeline (D5/D6/D9/D11).

``build_timeline`` is the ONE shared function both endpoints (D1) delegate
to, given an already-resolved ``IdentitySet`` (``identity.py``). It:

1. Queries each of the four source modules through their OWN repository
   (D5 -- zero SQL against another module's tables here), independently
   try/excepted (D6) so one source's failure never blanks the other three.
2. Normalizes every row into a common ``TimelineItem``.
3. Merges + sorts by ``occurred_at DESC`` with a stable tiebreaker (D9).
4. Truncates to ``limit`` and computes ``next_before``.

Identity resolution itself is NOT done here -- ``identity.py`` is called by
the routes layer, and a resolution failure there is a hard 500, never
reaching this function (D6's carve-out). Every failure caught in THIS
function is a source-read failure, not an identity failure, and is
therefore always degraded-200-eligible.
"""
from __future__ import annotations

from datetime import datetime

from common.auth import AuthClaims
from common.db import Database
from common.logging import get_logger

from api.config import get_api_settings
from api.conversation_store.repository import get_messages, list_conversations
from api.leads.repository import list_activities
from api.notifications.repository import list_jobs_for_leads
from api.scheduling.repository import list_events_for_timeline
from api.timeline.identity import IdentitySet
from api.timeline.models import SourceName, SourceOutcome, TimelineItem, TimelineResult

_log = get_logger(__name__)


async def _fetch_conversations(
    db: Database,
    claims: AuthClaims,
    identities: IdentitySet,
    *,
    before: datetime | None,
    limit: int,
) -> tuple[list[TimelineItem], bool]:
    """Resolve conversations by visitor identity, then messages per conversation (D11/J7).

    Returns ``(items, truncated)`` -- ``truncated`` is True if ANY resolved
    conversation had more messages in-window than
    ``TIMELINE_MESSAGES_PER_CONVERSATION`` (D11).
    """
    if not identities.visitor_ids:
        return [], False

    settings = get_api_settings()
    per_conv_cap = settings.timeline_messages_per_conversation

    # No started_to filter: a conversation that started before `before` may
    # still have older messages within the requested window even though the
    # conversation itself continued past it. Message-level filtering below
    # is the actual `before` boundary; the conversation list here is just
    # the candidate set to pull messages from. `limit` conversations is a
    # generous over-fetch consistent with D9's per-source over-fetch model.
    conversations, _total = await list_conversations(
        db,
        claims,
        limit=limit,
        visitor_ids=list(identities.visitor_ids),
    )

    items: list[TimelineItem] = []
    any_truncated = False
    for conv in conversations:
        all_messages = await get_messages(db, claims, conv.conversation_id)
        if before is not None:
            all_messages = [m for m in all_messages if m.created_at < before]
        # get_messages returns oldest->newest; keep the most-recent N (D11).
        conv_truncated = len(all_messages) > per_conv_cap
        kept = all_messages[-per_conv_cap:] if conv_truncated else all_messages
        if conv_truncated:
            any_truncated = True
        for msg in kept:
            items.append(
                TimelineItem(
                    kind="message",
                    occurred_at=msg.created_at,
                    item_id=msg.message_id,
                    data={
                        "role": msg.role,
                        "content": msg.content,
                        "conversation_id": conv.conversation_id,
                        "truncated": conv_truncated,
                    },
                )
            )
    return items, any_truncated


async def _fetch_lead_activities(
    db: Database,
    claims: AuthClaims,
    identities: IdentitySet,
    *,
    before: datetime | None,
    limit: int,
) -> list[TimelineItem]:
    """Fetch lead activities for every lead_id in the identity set (D2/J9)."""
    items: list[TimelineItem] = []
    for lead_id in identities.lead_ids:
        activities = await list_activities(db, claims, lead_id)
        for activity in activities:
            if before is not None and activity.created_at >= before:
                continue
            items.append(
                TimelineItem(
                    kind="lead_activity",
                    occurred_at=activity.created_at,
                    item_id=activity.activity_id,
                    data={
                        "type": activity.type,
                        "actor": activity.actor,
                        "payload": activity.payload,
                    },
                )
            )
    # Newest first, capped to limit -- list_activities has no LIMIT of its own.
    items.sort(key=lambda i: i.sort_key, reverse=True)
    return items[:limit]


async def _fetch_bookings(
    db: Database,
    claims: AuthClaims,
    identities: IdentitySet,
    *,
    before: datetime | None,
    limit: int,
) -> list[TimelineItem]:
    """Fetch schedule events reachable by lead_id OR visitor_id (D2/J8)."""
    if not identities.lead_ids and not identities.visitor_ids:
        return []

    events = await list_events_for_timeline(
        db,
        claims,
        lead_ids=list(identities.lead_ids),
        visitor_ids=list(identities.visitor_ids),
        before=before,
        limit=limit,
    )
    return [
        TimelineItem(
            kind="booking",
            occurred_at=event.created_at,
            item_id=event.event_id,
            data={
                "starts_at": event.starts_at.isoformat(),
                "status": event.status,
                "source": event.source,
            },
        )
        for event in events
    ]


async def _fetch_notifications(
    db: Database,
    claims: AuthClaims,
    identities: IdentitySet,
    *,
    before: datetime | None,
    limit: int,
) -> list[TimelineItem]:
    """Fetch notification jobs attributed to any lead_id in the set (D2/D4)."""
    if not identities.lead_ids:
        return []

    jobs = await list_jobs_for_leads(
        db, claims, lead_ids=list(identities.lead_ids), before=before, limit=limit,
    )
    return [
        TimelineItem(
            kind="notification",
            occurred_at=job.created_at,
            item_id=job.job_id,
            data={
                "channel": job.channel,
                "status": job.status,
                "subject": job.subject,
                # Never `body` (PII) -- leak-free per constraints.
            },
        )
        for job in jobs
    ]


async def build_timeline(
    db: Database,
    claims: AuthClaims,
    *,
    identities: IdentitySet,
    before: datetime | None,
    limit: int,
) -> TimelineResult:
    """Fan out to all four sources, merge, sort, paginate. Shared by both routes (D1).

    Each source is independently wrapped (D6): a raised exception marks that
    source ``unavailable`` in the result, sets ``degraded=True``, and logs a
    PII-free warning -- the other three sources' items are still returned.
    """
    sources: dict[SourceName, SourceOutcome] = {}
    all_items: list[TimelineItem] = []
    degraded = False

    # Each source fetch is called explicitly rather than through a
    # heterogeneously-typed dict of callables: `_fetch_conversations` returns
    # `(items, truncated)` (D11 needs the per-source truncation flag) while
    # the other three return `list[TimelineItem]`. A dict of these callables
    # forces mypy to infer a union return type it cannot narrow back per-call,
    # which is what the prior `# type: ignore` comments were papering over.
    # Explicit per-source blocks keep D6's independent-try/except contract
    # (one source's failure never blanks the others) with real static types.
    try:
        conv_items, conv_truncated = await _fetch_conversations(
            db, claims, identities, before=before, limit=limit,
        )
        sources["conversations"] = SourceOutcome(
            state="ok", count=len(conv_items), truncated=conv_truncated,
        )
        all_items.extend(conv_items)
    except Exception:
        degraded = True
        sources["conversations"] = SourceOutcome(state="unavailable")
        _log.warning(
            "timeline source unavailable",
            extra={"event": "timeline_source_unavailable", "source": "conversations"},
        )

    try:
        activity_items = await _fetch_lead_activities(
            db, claims, identities, before=before, limit=limit,
        )
        sources["lead_activities"] = SourceOutcome(state="ok", count=len(activity_items))
        all_items.extend(activity_items)
    except Exception:
        degraded = True
        sources["lead_activities"] = SourceOutcome(state="unavailable")
        _log.warning(
            "timeline source unavailable",
            extra={"event": "timeline_source_unavailable", "source": "lead_activities"},
        )

    try:
        booking_items = await _fetch_bookings(db, claims, identities, before=before, limit=limit)
        sources["bookings"] = SourceOutcome(state="ok", count=len(booking_items))
        all_items.extend(booking_items)
    except Exception:
        degraded = True
        sources["bookings"] = SourceOutcome(state="unavailable")
        _log.warning(
            "timeline source unavailable",
            extra={"event": "timeline_source_unavailable", "source": "bookings"},
        )

    try:
        notification_items = await _fetch_notifications(
            db, claims, identities, before=before, limit=limit,
        )
        sources["notifications"] = SourceOutcome(state="ok", count=len(notification_items))
        all_items.extend(notification_items)
    except Exception:
        degraded = True
        sources["notifications"] = SourceOutcome(state="unavailable")
        _log.warning(
            "timeline source unavailable",
            extra={"event": "timeline_source_unavailable", "source": "notifications"},
        )

    # D9: merge, sort by occurred_at DESC + stable tiebreaker, truncate to limit.
    all_items.sort(key=lambda item: item.sort_key, reverse=True)
    truncated_items = all_items[:limit]

    next_before = truncated_items[-1].occurred_at if len(truncated_items) == limit else None

    return TimelineResult(
        degraded=degraded,
        sources=sources,
        items=truncated_items,
        next_before=next_before,
    )

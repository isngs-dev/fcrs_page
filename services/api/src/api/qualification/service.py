"""Record an answer, rescore, alert the team once when a lead turns hot (plan 1.2/1.3).

The model only picks which configured choice an answer means (``classify``); the points
and the score are ``scoring``'s. Alert recipients are the missed-call list (owner,
2026-11-17); with nobody on it the alert waits, it isn't spent.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from common.auth import AuthClaims
from common.db import Database
from common.logging import get_logger

from api.call_agent import repository as call_repo
from api.contacts.repository import get_contact
from api.notifications.repository import enqueue_notification
from api.notifications.tasks import send_notification
from api.qualification import repository as repo
from api.qualification.scoring import QualConfig, band, engagement, score

_log = get_logger(__name__)

Classify = Callable[[str, list[str]], Awaitable[str]]
UNCLEAR = "unclear"


async def record_answer(db: Database, claims: AuthClaims, contact_id: str, key: str, heard: str,
                        *, channel: str, classify: Classify) -> str | None:
    """The choice ``heard`` maps to (saved, then rescored), or ``None`` when it maps to none."""
    config = await repo.get_config(db, claims)
    question = next((q for q in config.questions if q.key == key), None)
    if question is None:
        return None
    labels = [c.label for c in question.choices]
    picked = await classify(f"Question: {question.ask}\nAnswer: {heard}", [*labels, UNCLEAR])
    if picked not in labels:
        return None
    await repo.save_answer(db, claims, contact_id, key, choice=picked, heard=heard,
                           channel=channel, at=datetime.now(UTC))
    await rescore(db, claims, contact_id, config=config)
    return picked


async def rescore(db: Database, claims: AuthClaims, contact_id: str, *,
                  config: QualConfig | None = None) -> int:
    config = config or await repo.get_config(db, claims)
    record = await repo.get_record(db, claims, contact_id)
    value = score(config, record.chosen,
                  engagement(**await repo.engagement_signals(db, claims, contact_id)))
    await repo.save_score(db, claims, contact_id, score=value, band=band(value))
    if band(value) == "high" and record.alerted_at is None:
        await _alert(db, claims, contact_id, value, config)
    return value


async def rescore_safely(db: Database, claims: AuthClaims, contact_id: str) -> None:
    """For event handlers whose own work already committed (a reply handled, a lead
    planned): a failed rescore is logged, never retried with the whole event."""
    try:
        await rescore(db, claims, contact_id)
    except Exception as exc:  # noqa: BLE001 -- the next touch rescores; say it failed
        _log.warning("lead not rescored", extra={"event": "qualification_rescore_failed",
                                                 "reason": type(exc).__name__})


async def _alert(db: Database, claims: AuthClaims, contact_id: str, value: int,
                 config: QualConfig) -> None:
    calls = await call_repo.get_call_config(db, claims)
    recipients = calls.missed_call_alert_emails if calls else []
    if not recipients or not await repo.mark_alerted(db, claims, contact_id):
        return
    contact = await get_contact(db, claims, contact_id)
    record = await repo.get_record(db, claims, contact_id)
    who = (contact.name if contact else None) or (contact.phone if contact else None) or "A lead"
    lines = ["Hi,", "", f"{who} looks like a hot lead: their score is {value} of 100.", ""]
    if contact and contact.phone:
        lines.append(f"Phone: {contact.phone}")
    if contact and contact.email:
        lines.append(f"Email: {contact.email}")
    lines += ["", "What they told us:"]
    lines += [f"- {q.ask} {record.answers[q.key]['choice']}" for q in config.questions
              if q.key in record.answers]
    company = calls.company_name if calls else "your business"
    lines += ["", "Worth a personal call from your team soon.", "",
              f"— Rebecca, AI assistant for {company}"]
    for recipient in recipients:
        try:
            job_id = await enqueue_notification(
                db, claims, channel="email", recipient=recipient,
                subject=f"Hot lead: {who} (score {value})", body="\n".join(lines),
                dedupe_key=f"hot_lead_alert:{contact_id}:{recipient}",
                payload={"kind": "hot_lead_alert", "contact_id": contact_id},
                lead_id=contact.lead_id if contact else None)
            if job_id is not None:
                send_notification.delay(job_id=job_id, tenant_id=claims.tenant_id,
                                        correlation_id="")
        except Exception as exc:  # noqa: BLE001 -- the score stands; say the alert failed
            _log.warning("hot-lead alert not sent", extra={
                "event": "hot_lead_alert_failed", "reason": type(exc).__name__})

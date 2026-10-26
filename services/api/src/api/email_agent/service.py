"""Email Agent orchestration: prepare a task's email, send it, approve/discard drafts.

Status walk (E11): ``draft -> approved -> sending -> sent | failed``. The task
is ``pending`` while its draft awaits a human (E8) and ``completed`` once sent.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from common.auth import AuthClaims
from common.db import Database
from common.errors import ConflictError, NotFoundError, ValidationError
from common.logging import get_logger

from api.agents import compliance, memory, tools
from api.agents.dispatch import system_claims
from api.agents.events import backoff
from api.agents.executors import TransientExecutorError
from api.agents.outreach_repository import ClaimedTask, finish_task, reschedule_task
from api.agents.repository import get_agent_settings, start_run
from api.config import get_api_settings
from api.contacts.repository import get_contact, list_identities
from api.email_agent import repository as repo
from api.email_agent.composer import assemble_body, compose
from api.email_agent.config_repository import (
    EmailAgentConfig,
    get_email_agent_config,
    get_sendgrid_api_key,
)
from api.email_agent.links import link_secret, unsubscribe_url
from api.email_agent.providers import (
    OutboundEmail,
    SendGridEmailProvider,
    SmtpAgentEmailProvider,
    TransactionalEmailProvider,
)
from api.leads.repository import get_lead
from api.notifications.config_repository import get_notification_config
from api.scheduling.calendar_config_repository import get_calendar_config

_log = get_logger(__name__)


async def require_ready(
    db: Database, claims: AuthClaims, *, physical_address: str | None = None,
) -> EmailAgentConfig:
    """Everything a lawful send needs (E5) -- else ``EMAIL_NOT_READY``.

    ``physical_address`` checks a value about to be saved instead of the stored one.
    """
    config = await get_email_agent_config(db, claims)
    if config is None or not config.enabled:
        raise ValidationError("Agent email sender is not configured.", code="EMAIL_NOT_READY")
    if config.provider == "sendgrid" and not config.has_api_key:
        raise ValidationError("SendGrid API key is missing.", code="EMAIL_NOT_READY")
    address = physical_address or (await get_agent_settings(db, claims)).physical_address
    if not address:
        raise ValidationError("A physical postal address is required (CAN-SPAM).",
                              code="EMAIL_NOT_READY")
    link_secret()
    if not get_api_settings().public_api_base_url:
        raise ValidationError("PUBLIC_API_BASE_URL is not configured.", code="EMAIL_NOT_READY")
    return config


async def provider_for_tenant(db: Database, claims: AuthClaims,
                              config: EmailAgentConfig) -> TransactionalEmailProvider:
    timeout = get_api_settings().email_send_timeout_seconds
    if config.provider == "sendgrid":
        key = await get_sendgrid_api_key(db, claims)
        if not key:
            raise ValidationError("SendGrid API key is missing.", code="EMAIL_NOT_READY")
        return SendGridEmailProvider(key, timeout=timeout)
    smtp = await get_notification_config(db, claims, channel="email")
    if smtp is None or smtp.provider != "smtp" or not smtp.smtp_host or not smtp.smtp_port:
        raise ValidationError("SMTP sending needs the tenant's own SMTP email config.",
                              code="EMAIL_NOT_READY")
    return SmtpAgentEmailProvider(host=smtp.smtp_host, port=smtp.smtp_port,
                                  use_tls=smtp.smtp_use_tls, username=smtp.smtp_username,
                                  password=smtp.credentials, timeout=timeout)


async def _booking_line(db: Database, claims: AuthClaims, config: EmailAgentConfig) -> str:
    calendar = await get_calendar_config(db, claims)
    url = (calendar.scheduling_url if calendar is not None and calendar.provider == "calendly"
           else None) or config.booking_url
    if url:
        return f"Book a time that suits you: {url}"
    return "Just reply to this email and we'll find a time that suits you."


@dataclass(frozen=True)
class Prepared:
    status: str  # sent | draft | escalated
    message_id: str | None = None
    reason: str | None = None


async def prepare_and_maybe_send(
    db: Database, claims: AuthClaims, task: ClaimedTask, run_id: str,
) -> Prepared:
    """Executor body: compose (once per task), store, then send or park as a draft."""
    existing = await repo.get_message_for_task(db, claims, task.task_id)
    if existing is not None:  # redelivery / retry
        if existing.status in ("approved", "sending"):
            await send_message(db, claims, existing.message_id, run_id=run_id)
            return Prepared(status="sent", message_id=existing.message_id)
        return Prepared(status=existing.status, message_id=existing.message_id)

    config = await require_ready(db, claims)
    settings = await get_agent_settings(db, claims)
    contact = await get_contact(db, claims, task.contact_id)
    if contact is None or not contact.email:
        raise ValidationError("Contact has no email address.", code="CONTACT_NO_EMAIL")
    identities = await list_identities(db, claims, contact.contact_id)
    visitor_ids = [i.identity_value for i in identities if i.identity_type == "visitor_id"]
    if contact.lead_id:
        lead = await get_lead(db, claims, contact.lead_id)
        if lead is not None and lead.visitor_id:
            visitor_ids.append(lead.visitor_id)

    composed = await compose(db, claims, goal=task.goal, intent=task.intent,
                             first_name=(contact.name or "").split(" ")[0] or None,
                             visitor_ids=visitor_ids,
                             memory=await memory.render(db, claims, contact.contact_id,
                                                        include_chat=False))
    if composed.band == "escalate":
        _log.warning("agent email escalated", extra={"event": "agent_email_escalated",
                                                     "reason": composed.reason})
        return Prepared(status="escalated", reason=composed.reason)

    company = config.from_name or "our team"
    body = assemble_body(
        body=composed.body, booking=await _booking_line(db, claims, config), company=company,
        physical_address=str(settings.physical_address),
        unsubscribe_url=unsubscribe_url(str(claims.tenant_id), contact.contact_id, contact.email),
    )
    thread_id, thread_subject = await repo.get_or_create_thread(
        db, claims, contact_id=contact.contact_id, plan_id=task.plan_id, subject=composed.subject)
    references = await repo.thread_message_ids(db, claims, thread_id)
    subject = f"Re: {thread_subject}" if references else composed.subject
    domain = config.from_address.rpartition("@")[2]
    status = "approved" if settings.email_mode == "auto_send" else "draft"
    message_id = await repo.insert_message(
        db, claims, thread_id=thread_id, rfc_message_id=f"<{uuid4().hex}@{domain}>",
        in_reply_to=references[-1] if references else None, references=references,
        from_addr=config.from_address, to_addr=contact.email.strip().lower(), subject=subject,
        body_text=body, status=status, band=composed.band, sources=composed.sources,
        task_id=task.task_id,
    )
    if status == "draft":
        return Prepared(status="draft", message_id=message_id)
    await send_message(db, claims, message_id, run_id=run_id)
    return Prepared(status="sent", message_id=message_id)


async def send_message(db: Database, claims: AuthClaims, message_id: str, *, run_id: str) -> None:
    """Send an ``approved`` (or retry a ``sending``) message, then write back (E12)."""
    message = await repo.get_message(db, claims, message_id)
    if message is None:
        raise NotFoundError("Email not found.", code="EMAIL_NOT_FOUND")
    if message.status not in ("approved", "sending"):
        raise ConflictError(f"Email is {message.status}, not approved.", code="EMAIL_NOT_APPROVED")
    try:
        config = await require_ready(db, claims)
        provider = await provider_for_tenant(db, claims, config)
    except ValidationError as exc:  # misconfigured sender: fail it visibly, never 'approved'
        await repo.set_status(db, claims, message_id, status="failed",
                              from_statuses=("approved", "sending"), error=exc.code)
        raise
    await repo.set_status(db, claims, message_id, status="sending",
                          from_statuses=("approved", "sending"))
    try:
        sent = await provider.send(OutboundEmail(
            from_address=message.from_addr, from_name=config.from_name, to=message.to_addr,
            subject=message.subject, body_text=message.body_text,
            message_id=message.rfc_message_id,
            # A3 F2: with a reply domain, replies come back to the inbound webhook.
            reply_to=(f"reply+{message.thread_id}@{config.reply_domain}" if config.reply_domain
                      else config.reply_to),
            in_reply_to=message.in_reply_to, references=message.references,
            unsubscribe_url=unsubscribe_url(str(claims.tenant_id), message.contact_id or "",
                                            message.to_addr),
            custom_args={"message_id": message.message_id},
        ))
    except ValidationError as exc:  # deterministic: never retried
        await repo.set_status(db, claims, message_id, status="failed",
                              from_statuses=("sending",), error=exc.code)
        raise
    await repo.mark_sent(db, claims, message_id, provider=sent.provider,
                         provider_message_id=sent.provider_message_id)
    cost = get_api_settings().email_cost_micros
    await db.execute(
        "INSERT INTO usage_costs (tenant_id, cost_id, kind, units, unit, cost_micros, ref_type, "
        "ref_id) VALUES ($1, $2, 'email', 1, 'message', $3, 'email_message', $4)",
        claims.tenant_id, uuid4().hex, cost, message_id,
    )
    contact = await get_contact(db, claims, message.contact_id) if message.contact_id else None
    if contact is not None and contact.lead_id:
        await tools.invoke(db, claims, run_id, "log_activity", {
            "contact_id": message.contact_id, "type": "agent_email_sent",
            "summary": f"Email sent: {message.subject}"})


async def approve(db: Database, claims: AuthClaims, message_id: str) -> dict[str, str | None]:
    """Human approval: re-run the gate (consent/suppression may have changed), then send."""
    message = await repo.get_message(db, claims, message_id)
    if message is None:
        raise NotFoundError("Email not found.", code="EMAIL_NOT_FOUND")
    if message.status != "draft" or message.contact_id is None:
        raise ConflictError(f"Email is {message.status}, not a draft.", code="EMAIL_NOT_DRAFT")
    run_id = await start_run(db, claims, agent="email")
    now = datetime.now(UTC)
    # An in-thread answer to someone who wrote to us is a reply, not outreach (A3 F9).
    purpose: compliance.Purpose = (
        "reply" if message.task_id is None and message.in_reply_to else "outreach")
    gate = await compliance.check(db, claims, contact_id=message.contact_id, channel="email",
                                  now=now, run_id=run_id, purpose=purpose)
    if not gate.ok and gate.code in compliance.PERMANENT_CODES:
        await _close(db, claims, message, status="discarded", task_status="cancelled",
                     reason=gate.code)
        raise ConflictError("Sending is no longer allowed for this contact.",
                            code=gate.code or "GATE_BLOCKED")
    if not gate.ok:
        raise ConflictError("Sending is blocked right now; try again later.",
                            code=gate.code or "GATE_BLOCKED")
    if not await repo.set_status(db, claims, message_id, status="approved",
                                 from_statuses=("draft",), approved_by=claims.subject):
        raise ConflictError("Email changed while approving.", code="EMAIL_NOT_DRAFT")
    try:
        await send_message(db, claims, message_id, run_id=run_id)
    except ValidationError as exc:
        await _finish_task(db, claims, message, status="failed", reason=exc.code)
        raise
    except TransientExecutorError:
        # Provider hiccup: hand the task to the dispatcher, which resends the same
        # message (status 'sending', same Message-ID) with backoff.
        if message.task_id is not None:
            await reschedule_task(db, claims, message.task_id, at=now + backoff(1),
                                  reason="EMAIL_SEND_RETRY")
        return {"message_id": message_id, "status": "queued"}
    await _finish_task(db, claims, message, status="completed", reason=None)
    return {"message_id": message_id, "status": "sent"}


async def discard(db: Database, claims: AuthClaims, message_id: str) -> None:
    message = await repo.get_message(db, claims, message_id)
    if message is None:
        raise NotFoundError("Email not found.", code="EMAIL_NOT_FOUND")
    if message.status != "draft":
        raise ConflictError(f"Email is {message.status}, not a draft.", code="EMAIL_NOT_DRAFT")
    await _close(db, claims, message, status="discarded", task_status="cancelled",
                 reason="DISCARDED")


async def _close(db: Database, claims: AuthClaims, message: repo.EmailMessageRow, *,
                 status: str, task_status: str, reason: str | None) -> None:
    await repo.set_status(db, claims, message.message_id, status=status,
                          from_statuses=("draft",), error=reason)
    await _finish_task(db, claims, message, status=task_status, reason=reason)


async def _finish_task(db: Database, claims: AuthClaims, message: repo.EmailMessageRow, *,
                       status: str, reason: str | None) -> None:
    if message.task_id is None:
        return
    await finish_task(db, claims, message.task_id, status=status,
                      result={"message_id": message.message_id}, error=reason)


async def expire_drafts(db: Database, *, ttl_days: int) -> int:
    """System-scoped (reaper tick): stale drafts -> discarded, their tasks -> cancelled (E8)."""
    expired = await repo.expire_drafts_system(db, ttl_days=ttl_days)
    for draft in expired:
        if draft.task_id is not None:
            await finish_task(db, system_claims(draft.tenant_id), draft.task_id,
                              status="cancelled", error="DRAFT_EXPIRED")
    return len(expired)

"""A contact's agent data -- export and erasure (A9 H9, compliance §6).

Erasure deletes plans + tasks, email threads + messages, calls + transcripts, and
the call recordings at Plivo FIRST (any Plivo failure aborts before a row is
touched). Consent and suppression records are kept: they are the proof of what
the person agreed to and the blocklist that keeps them from being contacted again.
"""
from __future__ import annotations

from typing import Any

from common.auth import AuthClaims
from common.db import Database
from common.errors import AppException, NotFoundError

from api.agents import memory
from api.agents.executors import TransientExecutorError
from api.agents.outreach_repository import delete_plans_for_contact, list_plans_for_contact
from api.agents.repository import list_consents, suppressions_for_values
from api.call_agent import repository as call_repo
from api.call_agent.service import telephony_for
from api.contacts.repository import get_contact, list_identities
from api.email_agent import repository as email_repo


class RecordingDeleteFailedError(AppException):
    code = "RECORDING_DELETE_FAILED"
    http_status = 502
    default_message = "A call recording couldn't be deleted at Plivo; nothing was erased. Retry."


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


async def _require(db: Database, claims: AuthClaims, contact_id: str) -> Any:
    contact = await get_contact(db, claims, contact_id)
    if contact is None:
        raise NotFoundError("Contact not found.", code="CONTACT_NOT_FOUND")
    return contact


async def export_agent_data(db: Database, claims: AuthClaims, contact_id: str) -> dict[str, Any]:
    contact = await _require(db, claims, contact_id)
    values = {i.identity_value.lower() if i.identity_type == "email" else i.identity_value
              for i in await list_identities(db, claims, contact_id)
              if i.identity_type in ("email", "phone")}
    if contact.email:
        values.add(contact.email.strip().lower())
    plans = await list_plans_for_contact(db, claims, contact_id)
    emails = await email_repo.list_messages(db, claims, status=None, limit=200, offset=0,
                                            contact_id=contact_id)
    calls = await call_repo.list_sessions(db, claims, status=None, limit=200, offset=0,
                                          contact_id=contact_id)
    for call in calls:
        call["transcript"] = await call_repo.list_turns(db, claims, call["call_id"])
    return {
        "contact_id": contact_id,
        "plans": [{**{k: _iso(v) for k, v in p.items() if k != "tasks"},
                   "tasks": [{k: _iso(v) for k, v in t.__dict__.items()} for t in p["tasks"]]}
                  for p in plans],
        "consents": [{k: _iso(v) for k, v in c.__dict__.items()}
                     for c in await list_consents(db, claims, contact_id)],
        "suppressions": [{"channel": s.channel, "value": s.value_normalised, "reason": s.reason,
                          "created_at": _iso(s.created_at)}
                         for s in await suppressions_for_values(db, claims, sorted(values))],
        "memory": await memory.items(db, claims, contact_id),
        "emails": [{k: _iso(v) for k, v in m.__dict__.items()} for m in emails],
        "calls": [{k: ([{kk: _iso(vv) for kk, vv in t.items()} for t in v]
                       if k == "transcript" else _iso(v)) for k, v in c.items()} for c in calls],
    }


async def erase_agent_data(db: Database, claims: AuthClaims, contact_id: str) -> dict[str, Any]:
    await _require(db, claims, contact_id)
    recordings = await call_repo.contact_recordings(db, claims, contact_id)
    if recordings:
        config = await call_repo.get_call_config(db, claims)
        try:
            if config is None:
                raise RecordingDeleteFailedError()
            provider = await telephony_for(db, str(claims.tenant_id), config)
            for rec in recordings:
                if rec["recording_id"]:
                    await provider.delete_recording(recording_id=rec["recording_id"])
        except (AppException, TransientExecutorError) as exc:
            raise RecordingDeleteFailedError() from exc
    async with db.acquire() as conn, conn.transaction():
        plans = await delete_plans_for_contact(conn, claims, contact_id)
        threads = await email_repo.delete_for_contact(conn, claims, contact_id)
        calls = await call_repo.delete_for_contact(conn, claims, contact_id)
        await conn.execute("DELETE FROM contact_memory WHERE tenant_id = $1 AND contact_id = $2",
                           claims.tenant_id, contact_id)  # A11 M7
    return {"contact_id": contact_id, "plans": plans, "email_threads": threads, "calls": calls,
            "recordings_deleted": len(recordings)}

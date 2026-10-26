"""Agent tool registry: validated input, traced call, audited writes, errors never swallowed.

Every call writes an ``agent_tool_calls`` row with arguments redacted (emails ->
``***@domain``, phone numbers -> last two digits). Write tools also write an
``audit_events`` row.
"""
from __future__ import annotations

import re
from typing import Any, ClassVar, Protocol

from common.auth import AuthClaims
from common.db import Database
from common.errors import AppException, NotFoundError, ValidationError
from pydantic import BaseModel, Field
from pydantic import ValidationError as PydanticValidationError

from api.agents.repository import latest_consent, record_tool_call
from api.audit.repository import record_audit
from api.contacts.repository import get_contact, list_identities
from api.leads.repository import add_activity
from api.rag.service import retrieve_hybrid

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
_PHONE = re.compile(r"\+?\(?\d[\d\s().-]{6,}\d")


def redact_text(value: str) -> str:
    value = _EMAIL.sub(lambda m: f"***@{m.group(1)}", value)
    return _PHONE.sub(lambda m: "***" + re.sub(r"\D", "", m.group(0))[-2:], value)


def redact(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


class Tool(Protocol):
    name: ClassVar[str]
    write: ClassVar[bool]
    InputModel: ClassVar[type[BaseModel]]

    async def run(self, db: Database, claims: AuthClaims, args: Any) -> dict[str, Any]: ...


# -- Tools -----------------------------------------------------------------------


class SearchKnowledgeInput(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    k: int = Field(default=5, ge=1, le=20)


class SearchKnowledge:
    name: ClassVar[str] = "search_knowledge"
    write: ClassVar[bool] = False
    InputModel: ClassVar[type[BaseModel]] = SearchKnowledgeInput

    async def run(self, db: Database, claims: AuthClaims, args: Any) -> dict[str, Any]:
        result = await retrieve_hybrid(db, claims, args.query, k=args.k)
        return {
            "confidence": result.confidence,
            "passages": [{"chunk_id": c.chunk_id, "doc_id": c.doc_id, "content": c.content}
                         for c in result.chunks],
        }


class GetContactInput(BaseModel):
    contact_id: str = Field(min_length=1, max_length=64)


class GetContact:
    name: ClassVar[str] = "get_contact"
    write: ClassVar[bool] = False
    InputModel: ClassVar[type[BaseModel]] = GetContactInput

    async def run(self, db: Database, claims: AuthClaims, args: Any) -> dict[str, Any]:
        contact = await get_contact(db, claims, args.contact_id)
        if contact is None:
            raise NotFoundError("Contact not found.", code="CONTACT_NOT_FOUND")
        consents = {}
        for channel in ("email", "call"):
            c = await latest_consent(db, claims, contact.contact_id, channel)
            consents[channel] = None if c is None else {"status": c.status, "basis": c.basis}
        return {
            "contact_id": contact.contact_id,
            "name": contact.name,
            "email": contact.email,
            "phone": contact.phone,
            "lead_id": contact.lead_id,
            "identities": [{"type": i.identity_type, "value": i.identity_value}
                           for i in await list_identities(db, claims, contact.contact_id)],
            "consents": consents,
        }


class LogActivityInput(BaseModel):
    contact_id: str = Field(min_length=1, max_length=64)
    type: str = Field(pattern=r"^agent_[a-z_]{1,40}$")
    summary: str = Field(min_length=1, max_length=2000)


class LogActivity:
    name: ClassVar[str] = "log_activity"
    write: ClassVar[bool] = True
    InputModel: ClassVar[type[BaseModel]] = LogActivityInput

    async def run(self, db: Database, claims: AuthClaims, args: Any) -> dict[str, Any]:
        contact = await get_contact(db, claims, args.contact_id)
        if contact is None:
            raise NotFoundError("Contact not found.", code="CONTACT_NOT_FOUND")
        if contact.lead_id is None:
            # ponytail: contacts without a lead have no timeline table yet; A2 adds one if needed.
            raise ValidationError("Contact has no lead to log activity on.", code="NO_LEAD")
        activity_id = await add_activity(
            db, claims, contact.lead_id, type=args.type, payload={"summary": args.summary},
            actor=claims.subject,
        )
        return {"activity_id": activity_id, "lead_id": contact.lead_id}


TOOLS: dict[str, Tool] = {t.name: t for t in (SearchKnowledge(), GetContact(), LogActivity())}


async def invoke(
    db: Database, claims: AuthClaims, run_id: str, tool_name: str, args: dict[str, Any],
) -> dict[str, Any]:
    """Validate, run, trace. Errors are traced, then re-raised."""
    tool = TOOLS.get(tool_name)
    if tool is None:
        raise ValidationError(f"Unknown tool '{tool_name}'.", code="UNKNOWN_TOOL")
    redacted = redact(args)
    try:
        parsed = tool.InputModel.model_validate(args)
    except PydanticValidationError as exc:
        await record_tool_call(db, claims, run_id, tool=tool.name, args_redacted=redacted,
                               result_summary=None, ok=False, error_code="TOOL_INPUT_INVALID")
        raise ValidationError("Invalid tool input.", code="TOOL_INPUT_INVALID") from exc
    try:
        output = await tool.run(db, claims, parsed)
    except Exception as exc:
        code = exc.code if isinstance(exc, AppException) else type(exc).__name__
        await record_tool_call(db, claims, run_id, tool=tool.name, args_redacted=redacted,
                               result_summary=None, ok=False, error_code=code)
        raise
    audit_id = None
    if tool.write:
        audit_id = await record_audit(db, claims, action=f"agent_tool.{tool.name}",
                                      target_type="agent_run", target_id=run_id,
                                      metadata={"args": redacted})
    await record_tool_call(db, claims, run_id, tool=tool.name, args_redacted=redacted,
                           result_summary={"keys": sorted(output)}, ok=True,
                           audit_event_id=audit_id)
    return output

"""Outbound feeding API (A5): CSV import, add one contact, "Talk to Rebecca".

CLIENT_ADMIN and CLIENT_AGENT; PLATFORM_ADMIN via ``/admin/tenants/{tenant_id}/outreach``.
A CLIENT_AGENT's imported/added contacts are owned by them, and their requests
only ever reach contacts they can see.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from common.auth import AuthClaims, Role
from common.errors import NotFoundError
from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from api.agents import commands, import_service
from api.agents.imports import MAX_BYTES, rejected_csv
from api.auth.dependencies import require_roles, resolve_tenant_scope

router = APIRouter(prefix="/admin/outreach", tags=["outreach"])
tenant_scoped_router = APIRouter(prefix="/admin/tenants/{tenant_id}/outreach", tags=["outreach"])


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _import_out(row: dict[str, Any]) -> dict[str, Any]:
    return {k: _iso(v) for k, v in row.items()}


class CommitBody(BaseModel):
    consent_attested: bool = False
    start_outreach: bool = True


class AddContactBody(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    email: str | None = Field(default=None, max_length=320)
    phone: str | None = Field(default=None, max_length=40)
    timezone: str | None = Field(default=None, max_length=64)
    call_consent: bool = False
    consent_date: str | None = Field(default=None, max_length=30)
    consent_source: str | None = Field(default=None, max_length=300)
    consent_wording: str | None = Field(default=None, max_length=2000)
    consent_type: Literal["express_written", "express"] = "express_written"
    consent_attested: bool = False
    start_outreach: bool = True
    goal: str | None = Field(default=None, max_length=200)


class CommandBody(BaseModel):
    text: str = Field(min_length=3, max_length=2000)


def _mount(r: APIRouter, staff: Callable[..., Any]) -> None:

    @r.post("/imports", status_code=201)
    async def upload(
        request: Request,
        file: UploadFile = File(...),  # noqa: B008
        campaign_name: str = Form(..., min_length=1, max_length=120),
        goal: str | None = Form(default=None, max_length=200),
        claims: AuthClaims = Depends(staff),  # noqa: B008
    ) -> dict[str, Any]:
        data = await file.read(MAX_BYTES + 1)
        preview = await import_service.create_preview(
            request.app.state.db, claims, data=data, filename=file.filename,
            campaign_name=campaign_name.strip(), goal=goal)
        return _import_out(preview)

    @r.get("/imports/{import_id}")
    async def get_import(import_id: str, request: Request,
                         claims: AuthClaims = Depends(staff)) -> dict[str, Any]:  # noqa: B008
        row = await import_service.get_import(request.app.state.db, claims, import_id)
        if row is None:
            raise NotFoundError("Import not found.", code="IMPORT_NOT_FOUND")
        return _import_out(row)

    @r.get("/imports/{import_id}/rejected.csv")
    async def rejected(import_id: str, request: Request,
                       claims: AuthClaims = Depends(staff)) -> Response:  # noqa: B008
        rows = await import_service.rejected_rows(request.app.state.db, claims, import_id)
        if rows is None:
            raise NotFoundError("Rejected rows are only kept while the import is a preview.",
                                code="IMPORT_NOT_FOUND")
        return Response(rejected_csv(rows), media_type="text/csv", headers={
            "Content-Disposition": f'attachment; filename="rejected-{import_id[:8]}.csv"'})

    @r.post("/imports/{import_id}:commit")
    async def commit(import_id: str, body: CommitBody, request: Request,
                     claims: AuthClaims = Depends(staff)) -> dict[str, Any]:  # noqa: B008
        db = request.app.state.db
        if await import_service.get_import(db, claims, import_id) is None:
            raise NotFoundError("Import not found.", code="IMPORT_NOT_FOUND")
        report = await import_service.commit_import(
            db, claims, import_id, consent_attested=body.consent_attested,
            start_outreach=body.start_outreach)
        return {"import_id": import_id, "status": "committed", "report": report}

    @r.post("/imports/{import_id}:discard")
    async def discard(import_id: str, request: Request,
                      claims: AuthClaims = Depends(staff)) -> dict[str, str]:  # noqa: B008
        if not await import_service.discard_import(request.app.state.db, claims, import_id):
            raise NotFoundError("No preview import with that id.", code="IMPORT_NOT_FOUND")
        return {"import_id": import_id, "status": "discarded"}

    @r.post("/contacts:add", status_code=201)
    async def add_contact(body: AddContactBody, request: Request,
                          claims: AuthClaims = Depends(staff)) -> dict[str, Any]:  # noqa: B008
        values = {k: str(v) for k, v in body.model_dump(include={
            "name", "email", "phone", "timezone", "consent_date", "consent_source",
            "consent_wording", "consent_type"}).items() if v}
        if body.call_consent:
            values["call_consent"] = "yes"
        return await import_service.add_contact(
            request.app.state.db, claims, values=values, consent_attested=body.consent_attested,
            start_outreach=body.start_outreach, goal=body.goal)

    @r.post("/commands", status_code=201)
    async def command(body: CommandBody, request: Request,
                      claims: AuthClaims = Depends(staff)) -> dict[str, Any]:  # noqa: B008
        return await commands.create_draft(request.app.state.db, claims, body.text)

    @r.get("/commands/{draft_id}")
    async def get_command(draft_id: str, request: Request,
                          claims: AuthClaims = Depends(staff)) -> dict[str, Any]:  # noqa: B008
        row = await commands.get_draft(request.app.state.db, claims, draft_id)
        if row is None:
            raise NotFoundError("Request not found.", code="COMMAND_NOT_FOUND")
        return {k: _iso(v) for k, v in row.items() if k != "contact_ids"} | {
            "contact_count": len(row["contact_ids"])}

    @r.post("/commands/{draft_id}:confirm")
    async def confirm(draft_id: str, request: Request,
                      claims: AuthClaims = Depends(staff)) -> dict[str, Any]:  # noqa: B008
        return await commands.confirm_draft(request.app.state.db, claims, draft_id)

    @r.post("/commands/{draft_id}:discard")
    async def discard_command(draft_id: str, request: Request,
                              claims: AuthClaims = Depends(staff)) -> dict[str, str]:  # noqa: B008
        await commands.discard_draft(request.app.state.db, claims, draft_id)
        return {"draft_id": draft_id, "status": "discarded"}


_mount(router, require_roles(Role.CLIENT_ADMIN, Role.CLIENT_AGENT))
_mount(tenant_scoped_router, resolve_tenant_scope(Role.CLIENT_ADMIN))

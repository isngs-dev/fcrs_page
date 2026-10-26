"""CSV import + add-one-contact service (A5 J1, J3-J9).

Preview stores the parsed rows; commit turns them into contacts, consent records
and one campaign of plans; discard drops them. Row data is cleared once the
import is committed or discarded (only the report stays).
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from common.auth import AuthClaims, Role
from common.db import Database
from common.errors import ConflictError, ValidationError

from api.agents import planner
from api.agents.imports import ImportRow, check_row, parse_csv
from api.agents.models import PlanKind
from api.agents.outreach_repository import create_campaign
from api.agents.repository import _reject_global, add_consent, is_suppressed
from api.audit.repository import record_audit
from api.contacts.repository import (
    add_identity,
    create_contact,
    get_contact_id_by_identity,
    get_contact_timezone,
    set_contact_timezone,
)


async def _existing_contact(db: Database, claims: AuthClaims, row: ImportRow) -> str | None:
    for kind, value in (("email", row.email), ("phone", row.phone)):
        if value:
            found = await get_contact_id_by_identity(db, claims, identity_type=kind,
                                                     identity_value=value)
            if found:
                return found
    return None


async def _suppressed(db: Database, claims: AuthClaims, row: ImportRow) -> bool:
    return bool((row.email and await is_suppressed(db, claims, channel="email",
                                                    values=[row.email]))
                or (row.phone and await is_suppressed(db, claims, channel="call",
                                                       values=[row.phone])))


# -- Preview / read / discard -------------------------------------------------------------


async def create_preview(db: Database, claims: AuthClaims, *, data: bytes,
                         filename: str | None, campaign_name: str, goal: str | None
                         ) -> dict[str, Any]:
    _reject_global(claims)
    parsed = parse_csv(data)
    accepted: list[ImportRow] = []
    existing = 0
    rejected = [asdict(r) for r in parsed.rejected]
    # ponytail: a few queries per row, sequential; batch these if 5k-row files get slow.
    for row in parsed.rows:
        if await _suppressed(db, claims, row):
            rejected.append({"row": row.row, "reasons": ["SUPPRESSED"], "raw": {}})
            continue
        existing += int(await _existing_contact(db, claims, row) is not None)
        accepted.append(row)
    rejected.sort(key=lambda r: r["row"])
    parsed.rows = accepted
    report = parsed.report() | {
        "rejected": [{"row": r["row"], "reasons": r["reasons"]} for r in rejected],
        "rejected_count": len(rejected), "valid": len(accepted), "existing": existing,
        "new": len(accepted) - existing,
    }
    import_id = uuid4().hex
    await db.execute(
        "INSERT INTO outreach_imports (tenant_id, import_id, filename, campaign_name, goal, rows, "
        "report, created_by) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
        claims.tenant_id, import_id, (filename or "")[:200] or None, campaign_name, goal,
        {"accepted": [asdict(r) for r in accepted], "rejected": rejected}, report,
        claims.subject,
    )
    return await get_import(db, claims, import_id) or {}


async def get_import(db: Database, claims: AuthClaims, import_id: str) -> dict[str, Any] | None:
    _reject_global(claims)
    row = await db.fetchrow(
        "SELECT import_id, status, filename, campaign_name, goal, report, consent_attested, "
        "attested_by, created_by, created_at, committed_at FROM outreach_imports "
        "WHERE tenant_id = $1 AND import_id = $2",
        claims.tenant_id, import_id,
    )
    return dict(row) if row is not None else None


async def rejected_rows(db: Database, claims: AuthClaims, import_id: str
                        ) -> list[dict[str, Any]] | None:
    """Rejected rows with their original cells -- only while the import is a preview."""
    _reject_global(claims)
    rows = await db.fetchval(
        "SELECT rows FROM outreach_imports WHERE tenant_id = $1 AND import_id = $2 "
        "AND status = 'preview'",
        claims.tenant_id, import_id,
    )
    return list(rows.get("rejected", [])) if rows is not None else None


async def discard_import(db: Database, claims: AuthClaims, import_id: str) -> bool:
    _reject_global(claims)
    tag = await db.execute(
        "UPDATE outreach_imports SET status = 'discarded', rows = '{}' "
        "WHERE tenant_id = $1 AND import_id = $2 AND status = 'preview'",
        claims.tenant_id, import_id,
    )
    return str(tag).endswith(" 1")


# -- Commit ------------------------------------------------------------------------------


async def _apply_row(db: Database, claims: AuthClaims, row: ImportRow, *, import_id: str | None,
                     campaign_id: str | None, kind: PlanKind, goal: str | None,
                     now: datetime) -> dict[str, str]:
    """Contact (reuse or create) -> consents -> plan. Returns what happened."""
    contact_id = await _existing_contact(db, claims, row)
    created = contact_id is None
    if contact_id is None:
        contact_id = await create_contact(
            db, claims, account_id=None, lead_id=None, name=row.name, email=row.email,
            phone=row.phone, consent={"source": "csv" if import_id else "admin_form",
                                      "import_id": import_id},
            # A CLIENT_AGENT owns what they import, so they can see and manage it.
            owner_agent_id=claims.subject if claims.role is Role.CLIENT_AGENT else None)
        for identity_type, value in (("email", row.email), ("phone", row.phone)):
            if value:
                try:
                    await add_identity(db, claims, contact_id, identity_type=identity_type,
                                       identity_value=value)
                except ValidationError:
                    pass  # owned by another contact: this one still gets the plan
    if row.timezone and await get_contact_timezone(db, claims, contact_id) is None:
        await set_contact_timezone(db, claims, contact_id, row.timezone)

    source = "csv" if import_id else "admin_form"
    evidence: dict[str, Any] = {"import_id": import_id, "row": row.row}
    if row.email:
        await add_consent(db, claims, contact_id, channel="email", status="granted",
                          basis="documented_lawful_basis", source=source, evidence=evidence)
    if row.call_consent:
        c = row.call_consent
        await add_consent(
            db, claims, contact_id, channel="call", status="granted",
            basis="express_written_consent" if c["type"] == "express_written"
            else "express_consent",
            source=source, wording=c["wording"] or None,
            evidence=evidence | {"consent_date": c["date"], "consent_source": c["source"]})

    outcome = "not_started"
    if campaign_id is not None:
        try:
            plan = await planner.create_plan(db, claims, contact_id=contact_id, kind=kind,
                                             goal=goal, now=now, campaign_id=campaign_id)
            outcome = "planned" if plan.plan_id else "no_allowed_channel"
        except ConflictError:
            outcome = "plan_already_active"
    return {"contact_id": contact_id, "contact": "created" if created else "existing",
            "plan": outcome}


async def commit_import(db: Database, claims: AuthClaims, import_id: str, *,
                        consent_attested: bool, start_outreach: bool = True) -> dict[str, Any]:
    _reject_global(claims)
    current = await db.fetchrow(
        "SELECT status, rows, campaign_name, goal, report FROM outreach_imports "
        "WHERE tenant_id = $1 AND import_id = $2",
        claims.tenant_id, import_id,
    )
    if current is None:
        raise ValidationError("Import not found.", code="IMPORT_NOT_FOUND")
    if current["status"] != "preview":
        raise ConflictError(f"This import is already {current['status']}.",
                            code="IMPORT_NOT_PREVIEW")
    rows = [ImportRow(**r) for r in current["rows"].get("accepted", [])]
    if any(r.call_consent for r in rows) and not consent_attested:
        raise ValidationError("Confirm that the call-consent evidence in this file is genuine.",
                              code="CONSENT_ATTESTATION_REQUIRED")
    # Claim it (exactly once even if two people press Commit together).
    claimed = await db.execute(
        "UPDATE outreach_imports SET status = 'committed', committed_at = now(), "
        "consent_attested = $3, attested_by = CASE WHEN $3 THEN $4 ELSE NULL END "
        "WHERE tenant_id = $1 AND import_id = $2 AND status = 'preview'",
        claims.tenant_id, import_id, consent_attested, claims.subject,
    )
    if not str(claimed).endswith(" 1"):
        raise ConflictError("This import was just committed.", code="IMPORT_NOT_PREVIEW")

    now = datetime.now(UTC)
    campaign_id = None
    if start_outreach:
        campaign_id = await create_campaign(db, claims, name=current["campaign_name"],
                                            kind="outbound_csv", goal=current["goal"])
    counts: dict[str, int] = {}
    for row in rows:
        result = await _apply_row(db, claims, row, import_id=import_id, campaign_id=campaign_id,
                                  kind="outbound_csv", goal=current["goal"], now=now)
        for key in (f"contact_{result['contact']}", f"plan_{result['plan']}"):
            counts[key] = counts.get(key, 0) + 1
    report = dict(current["report"]) | {"committed": counts, "campaign_id": campaign_id}
    await db.execute(
        "UPDATE outreach_imports SET rows = '{}', report = $3 "
        "WHERE tenant_id = $1 AND import_id = $2",
        claims.tenant_id, import_id, report,
    )
    await record_audit(db, claims, action="outreach.import_committed",
                       target_type="outreach_import", target_id=import_id,
                       metadata={"rows": len(rows), "consent_attested": consent_attested,
                                 "call_consent_rows": sum(1 for r in rows if r.call_consent),
                                 **counts})
    return report


# -- Add one contact (J9) ----------------------------------------------------------------------


async def add_contact(db: Database, claims: AuthClaims, *, values: dict[str, str],
                      consent_attested: bool, start_outreach: bool,
                      goal: str | None) -> dict[str, Any]:
    _reject_global(claims)
    now = datetime.now(UTC)
    row, reasons = check_row(values, 1, now.date())
    if row is None:
        raise ValidationError("Check the contact details: " + ", ".join(reasons),
                              code=reasons[0])
    if await _suppressed(db, claims, row):
        raise ValidationError("This contact has opted out.", code="CONTACT_SUPPRESSED")
    if row.call_consent and not consent_attested:
        raise ValidationError("Confirm that the call-consent evidence is genuine.",
                              code="CONSENT_ATTESTATION_REQUIRED")
    campaign_id = None
    if start_outreach:
        campaign_id = await create_campaign(db, claims, name=f"Added {now:%Y-%m-%d}",
                                            kind="outbound_manual", goal=goal)
    result = await _apply_row(db, claims, row, import_id=None, campaign_id=campaign_id,
                              kind="outbound_manual", goal=goal, now=now)
    await record_audit(db, claims, action="outreach.contact_added", target_type="contact",
                       target_id=result["contact_id"],
                       metadata={"call_consent": bool(row.call_consent),
                                 "consent_attested": consent_attested, "plan": result["plan"]})
    return result | {"warnings": row.warnings}

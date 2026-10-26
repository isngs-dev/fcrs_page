"""Tamper-evident audit chain (A1 D10, sealer design).

``record_audit`` stays a single fast INSERT. A Beat task (``seal_audit_chain``)
links unsealed rows per tenant, in a short transaction holding that tenant's
``audit_chain_heads`` row lock: ``row_hash = sha256(prev_hash + canonical_json(row))``,
``chain_seq`` contiguous from 1. Editing or deleting a sealed row breaks the
chain, which ``verify_audit_chain`` reports. Rows written since the last seal
(seconds) are reported as ``unsealed``, not as verified.

Platform-level actions (no tenant) go under the reserved tenant id
``__platform__`` via ``record_platform_audit``.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import uuid4

from common.auth import AuthClaims, Role
from common.db import Database
from common.errors import AuthorizationError

from api.audit.repository import _reject_global

PLATFORM_TENANT = "__platform__"
GENESIS = "0" * 64


def _row_hash(prev_hash: str, row: Any) -> str:
    body = {
        "tenant_id": row["tenant_id"],
        "event_id": row["event_id"],
        "actor": row["actor"],
        "action": row["action"],
        "target_type": row["target_type"],
        "target_id": row["target_id"],
        "metadata": row["metadata"],
        "created_at": row["created_at"].isoformat(),
    }
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256((prev_hash + canonical).encode("utf-8")).hexdigest()


async def record_platform_audit(
    db: Database,
    claims: AuthClaims,
    *,
    action: str,
    target_type: str | None = None,
    target_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> str:
    """Audit a platform action (e.g. the global kill switch). PLATFORM_ADMIN only."""
    if claims.role is not Role.PLATFORM_ADMIN:
        raise AuthorizationError("Platform admin only.", code="ROLE_NOT_PERMITTED")
    event_id = uuid4().hex
    await db.execute(
        "INSERT INTO audit_events "
        "(tenant_id, event_id, actor, action, target_type, target_id, metadata) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7)",
        PLATFORM_TENANT, event_id, claims.subject, action, target_type, target_id, metadata,
    )
    return event_id


_ROW_SELECT = (
    "SELECT tenant_id, event_id, actor, action, target_type, target_id, metadata, created_at, "
    "prev_hash, row_hash, chain_seq FROM audit_events "
)


async def _seal_tenant(db: Database, tenant_id: str, *, limit: int) -> int:
    async with db.acquire() as conn, conn.transaction():
        await conn.execute(
            "INSERT INTO audit_chain_heads (tenant_id, last_hash, last_seq) VALUES ($1, $2, 0) "
            "ON CONFLICT (tenant_id) DO NOTHING",
            tenant_id, GENESIS,
        )
        head = await conn.fetchrow(
            "SELECT last_hash, last_seq FROM audit_chain_heads WHERE tenant_id = $1 FOR UPDATE",
            tenant_id,
        )
        rows = await conn.fetch(
            _ROW_SELECT + "WHERE tenant_id = $1 AND row_hash IS NULL "
            "ORDER BY created_at, event_id LIMIT $2 FOR UPDATE",
            tenant_id, limit,
        )
        prev, seq = str(head["last_hash"]), int(head["last_seq"])
        for row in rows:
            seq += 1
            row_hash = _row_hash(prev, row)
            await conn.execute(
                "UPDATE audit_events SET prev_hash = $3, row_hash = $4, chain_seq = $5 "
                "WHERE tenant_id = $1 AND event_id = $2",
                tenant_id, row["event_id"], prev, row_hash, seq,
            )
            prev = row_hash
        if rows:
            await conn.execute(
                "UPDATE audit_chain_heads SET last_hash = $2, last_seq = $3, updated_at = now() "
                "WHERE tenant_id = $1",
                tenant_id, prev, seq,
            )
        return len(rows)


async def seal_audit_chain(db: Database, *, limit: int = 500) -> int:
    """System-scoped: seal up to ``limit`` unsealed rows per tenant. Returns rows sealed."""
    tenants = await db.fetch(
        "SELECT DISTINCT tenant_id FROM audit_events WHERE row_hash IS NULL",
    )
    sealed = 0
    for t in tenants:
        sealed += await _seal_tenant(db, str(t["tenant_id"]), limit=limit)
    return sealed


@dataclass(frozen=True)
class ChainReport:
    first_chained_at: datetime | None
    rows_checked: int
    unsealed: int
    broken_at: int | None  # chain_seq of the first row that doesn't verify
    ok: bool


async def verify_audit_chain(db: Database, claims: AuthClaims) -> ChainReport:
    """Recompute the caller's tenant chain end to end."""
    _reject_global(claims)
    # ponytail: loads the whole chain; page by chain_seq when a tenant has ~1M rows.
    rows = await db.fetch(
        _ROW_SELECT + "WHERE tenant_id = $1 AND chain_seq IS NOT NULL ORDER BY chain_seq",
        claims.tenant_id,
    )
    unsealed = int(await db.fetchval(
        "SELECT count(*) FROM audit_events WHERE tenant_id = $1 AND row_hash IS NULL",
        claims.tenant_id,
    ))
    head = await db.fetchrow(
        "SELECT last_hash, last_seq FROM audit_chain_heads WHERE tenant_id = $1",
        claims.tenant_id,
    )
    prev, broken = GENESIS, None
    for expected_seq, row in enumerate(rows, start=1):
        if (row["chain_seq"] != expected_seq or row["prev_hash"] != prev
                or row["row_hash"] != _row_hash(prev, row)):
            broken = expected_seq
            break
        prev = row["row_hash"]
    if broken is None and head is not None and (
            head["last_seq"] != len(rows) or head["last_hash"] != prev):
        broken = len(rows) + 1  # rows removed from the end
    return ChainReport(
        first_chained_at=rows[0]["created_at"] if rows else None,
        rows_checked=len(rows),
        unsealed=unsealed,
        broken_at=broken,
        ok=broken is None,
    )

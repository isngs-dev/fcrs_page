"""Contacts repository — tenant-scoped async SQL for the Contact entity and
its multi-identity table.

Every method:
- Takes ``AuthClaims`` as its first positional argument.
- Calls ``_reject_global(claims)`` to reject PLATFORM_ADMIN (no global scope).
- Uses positional placeholders numbered by position (``$1``, ``$2``, …).
- Never returns or accepts ``tenant_id`` in its public return types; that is
  an internal filter only.

Data model (migration 0040):
- ``contacts(tenant_id PK, contact_id PK, account_id?, lead_id?, name?,
  email?, phone?, consent jsonb NOT NULL, owner_agent_id?, created_at,
  updated_at)``. ``lead_id`` nullable (D2), ``account_id`` nullable (D3),
  ``consent`` NOT NULL (D7). A partial UNIQUE index on
  ``(tenant_id, lead_id) WHERE lead_id IS NOT NULL`` makes "two Contacts
  from one Lead" unrepresentable (D6) -- a direct second ``create_contact``
  call with the same ``lead_id`` raises (surfaced here as ``ValidationError``
  ``CONTACT_ALREADY_LINKED_TO_LEAD``, translating the raw DB
  ``UniqueViolationError`` rather than letting a 500 leak through).
- ``contact_identities(tenant_id PK, identity_id PK, contact_id, type,
  value, created_at)`` -- the full multi-identity set (D4). The UNIQUE index
  on ``(tenant_id, identity_type, identity_value)`` is tenant-scoped: the
  same identity value under two different tenants must succeed both times.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import uuid4

from asyncpg.exceptions import UniqueViolationError
from common.auth import AuthClaims
from common.db import Database
from common.errors import ValidationError


@dataclass(frozen=True)
class Contact:
    """A single durable contact (person) row."""

    contact_id: str
    account_id: str | None
    lead_id: str | None
    name: str | None
    email: str | None
    phone: str | None
    consent: dict[str, Any]
    owner_agent_id: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class ContactIdentity:
    """A single (contact_id, identity_type, identity_value) row."""

    identity_id: str
    contact_id: str
    identity_type: str
    identity_value: str
    created_at: datetime


def _reject_global(claims: AuthClaims) -> None:
    """Raise ``ValidationError`` for global callers (PLATFORM_ADMIN).

    Contacts are always tenant-scoped; a global caller has no tenant_id and
    therefore cannot be filtered to a tenant's rows.
    """
    if claims.tenant_id is None:
        raise ValidationError(
            "Contact repository is tenant-scoped; PLATFORM_ADMIN callers are not permitted.",
            code="GLOBAL_CALLER_NOT_PERMITTED",
        )


# SR-29: ORDER BY identifiers cannot be bound parameters in Postgres. The
# VALUES in this dict are the only SQL fragments ever accepted for list
# ordering; the caller's string is used exclusively as a dictionary KEY and
# is never interpolated into SQL. This dict-membership check is the ONLY
# thing preventing SQL injection on this path.
#
# D-COMPANY-SORT: "account" orders by the opaque account_id -- it GROUPS
# contacts by company (clustering same-account rows adjacently, and is
# index-assisted by idx_contacts_tenant_account), it does NOT alpha-sort by
# company name. A `LEFT JOIN accounts ORDER BY a.name` alternative was
# considered and deliberately not built this sprint (it would widen the
# tenant-isolation surface to a second table and complicate the count query)
# -- flagged as a future option, not built. The frontend must label this
# control "Group by company", never "Sort by company".
_SORT_COLUMNS: dict[str, str] = {
    "name": "name",
    "email": "email",
    "account": "account_id",
    "owner": "owner_agent_id",
    "created": "created_at",
}
CONTACT_SORT_KEYS = frozenset(_SORT_COLUMNS)

_DEFAULT_SORT = "created"
_DEFAULT_DIRECTION = "desc"
CONTACT_SORT_DEFAULT_DIRECTIONS: dict[str, str] = {
    "name": "asc",
    "email": "asc",
    "account": "asc",
    "owner": "asc",
    "created": "desc",
}


def _escape_ilike_pattern(value: str) -> str:
    """Escape user wildcard characters before binding a literal substring."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def create_contact(
    db: Database,
    claims: AuthClaims,
    *,
    account_id: str | None,
    lead_id: str | None,
    name: str | None,
    email: str | None,
    phone: str | None,
    consent: dict[str, Any],
    owner_agent_id: str | None = None,
) -> str:
    """Insert a new ``contacts`` row. Returns the ``contact_id`` (uuid4().hex).

    Raises ``ValidationError`` (``CONTACT_ALREADY_LINKED_TO_LEAD``) if
    ``lead_id`` is not ``None`` and a contact already exists for this
    ``(tenant_id, lead_id)`` pair (D6's partial unique index, proven
    independently of any route-level check).

    Does NOT validate that ``account_id``/``lead_id`` belong to this tenant
    -- callers (routes) are expected to have already resolved/validated them.
    An invalid composite FK reference raises a raw ``asyncpg`` foreign-key
    error, which is intentionally NOT swallowed here (routes validate first
    so this path should not be reached with a bad reference in practice).
    """
    _reject_global(claims)

    new_contact_id = uuid4().hex
    try:
        await db.execute(
            "INSERT INTO contacts "
            "(tenant_id, contact_id, account_id, lead_id, name, email, phone, "
            " consent, owner_agent_id) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
            claims.tenant_id,
            new_contact_id,
            account_id,
            lead_id,
            name,
            email,
            phone,
            consent,
            owner_agent_id,
        )
    except UniqueViolationError as exc:
        raise ValidationError(
            "A contact already exists for this lead.",
            code="CONTACT_ALREADY_LINKED_TO_LEAD",
        ) from exc
    return new_contact_id


async def get_contact(
    db: Database,
    claims: AuthClaims,
    contact_id: str,
) -> Contact | None:
    """Fetch a contact by ``contact_id`` scoped to the caller's tenant, or ``None``."""
    _reject_global(claims)

    row = await db.fetchrow(
        "SELECT contact_id, account_id, lead_id, name, email, phone, consent, "
        "owner_agent_id, created_at, updated_at "
        "FROM contacts "
        "WHERE tenant_id = $1 AND contact_id = $2",
        claims.tenant_id,
        contact_id,
    )
    return _row_to_contact(row) if row is not None else None


async def get_contact_by_lead_id(
    db: Database,
    claims: AuthClaims,
    lead_id: str,
) -> Contact | None:
    """Fetch the contact converted from a given lead, tenant-scoped, or ``None``.

    Used by the conversion route's D6 idempotency check to return the
    existing ``contact_id`` on a 409 rather than a bare error.
    """
    _reject_global(claims)

    row = await db.fetchrow(
        "SELECT contact_id, account_id, lead_id, name, email, phone, consent, "
        "owner_agent_id, created_at, updated_at "
        "FROM contacts "
        "WHERE tenant_id = $1 AND lead_id = $2",
        claims.tenant_id,
        lead_id,
    )
    return _row_to_contact(row) if row is not None else None


async def list_contacts(
    db: Database,
    claims: AuthClaims,
    *,
    limit: int = 50,
    offset: int = 0,
    q: str | None = None,
    account_id: str | None = None,
    sort: str = _DEFAULT_SORT,
    direction: str = _DEFAULT_DIRECTION,
) -> tuple[list[Contact], int]:
    """Fetch a paginated, sorted, optionally-filtered/searched page of the
    caller's tenant contacts.

    ``sort``/``direction`` are validated here (defense-in-depth; the route
    layer validates first) because ORDER BY structure cannot be bound as a
    parameter. ``q`` is a literal, case-insensitive Name-or-Email substring
    match -- wildcard characters are escaped before binding so a search for
    a literal ``%`` or ``_`` does not silently become a match-all/match-any
    pattern. ``account_id`` is a real, indexed exact-match filter and is
    DELIBERATELY NOT existence-validated here (unlike the write path's
    ``_validate_account_id``) -- an unknown or cross-tenant id simply yields
    zero rows via the tenant-scoped WHERE, which is an honest empty result,
    not a caller error (D-FILTER).

    Returns ``(rows, total)`` -- ``total`` is a ``count(*)`` over the same
    filtered WHERE (minus LIMIT/OFFSET), so it stays consistent with
    ``items`` under search/filter. Ordered by the SR-29 closed allowlist
    with ``contact_id DESC`` as the mandatory total-order tie-break
    (required for pagination correctness, not cosmetic).
    """
    _reject_global(claims)

    if sort not in _SORT_COLUMNS:
        raise ValidationError(f"Unknown sort key {sort!r}.", code="INVALID_CONTACT_SORT")
    if direction not in {"asc", "desc"}:
        raise ValidationError(
            f"Unknown sort direction {direction!r}.", code="INVALID_CONTACT_SORT_DIRECTION",
        )

    where = "WHERE tenant_id = $1"
    params: list[Any] = [claims.tenant_id]

    if account_id is not None:
        params.append(account_id)
        where += f" AND account_id = ${len(params)}"

    if q:
        escaped_q = _escape_ilike_pattern(q)
        params.extend([f"%{escaped_q}%", f"%{escaped_q}%"])
        name_idx = len(params) - 1
        email_idx = len(params)
        where += (
            f" AND (name ILIKE ${name_idx} ESCAPE '\\' "
            f"OR email ILIKE ${email_idx} ESCAPE '\\')"
        )

    sort_expr = _SORT_COLUMNS[sort]  # constant; never caller input
    direction_sql = "ASC" if direction == "asc" else "DESC"
    # D-NULLS: NULLS LAST in BOTH directions -- an absent value is not a low value.
    # D-TIEBREAK: `contact_id DESC` is MANDATORY; without a total order, pagination
    # can silently duplicate or skip rows across pages.
    order_by = f"ORDER BY {sort_expr} {direction_sql} NULLS LAST, contact_id DESC"

    # Parameterized SQL; `where` is built above from bound params only and
    # `order_by` is constrained to the developer-authored allowlist above.
    # ruff: noqa: S608
    count_row = await db.fetchrow(
        "SELECT count(*) AS count FROM contacts " + where, *params,
    )
    total = int(count_row["count"]) if count_row is not None else 0

    clamped_limit = max(1, min(limit, 200))
    page_params = [*params, clamped_limit, max(0, offset)]
    limit_idx = len(page_params) - 1
    offset_idx = len(page_params)
    rows = await db.fetch(
        "SELECT contact_id, account_id, lead_id, name, email, phone, consent, "
        "owner_agent_id, created_at, updated_at "
        "FROM contacts " + where + " "
        f"{order_by} LIMIT ${limit_idx} OFFSET ${offset_idx}",
        *page_params,
    )
    return [_row_to_contact(row) for row in rows], total


_UNSET: Any = object()
"""Sentinel meaning "field not supplied" for ``update_contact``'s partial
update. Distinct from ``None``, which is a valid, explicit "clear this
field" value (e.g. ``account_id=None`` unlinks the account) -- a plain
default of ``None`` could not distinguish the two cases."""


async def update_contact(
    db: Database,
    claims: AuthClaims,
    contact_id: str,
    *,
    name: str | None = _UNSET,
    email: str | None = _UNSET,
    phone: str | None = _UNSET,
    account_id: str | None = _UNSET,
) -> Contact | None:
    """Partially update a contact, tenant-scoped. Only supplied fields change.

    Uses the ``_UNSET`` sentinel as the "not supplied" default so ``None``
    can be passed explicitly to unlink/clear a field (e.g.
    ``account_id=None`` unlinks the account) -- the K10 positional-param
    idiom, extended to support partial updates. Returns the updated
    ``Contact``, or ``None`` if no row matched (missing ``contact_id`` or
    cross-tenant access).
    """
    _reject_global(claims)

    set_clauses: list[str] = []
    params: list[Any] = []

    for column, value in (
        ("name", name),
        ("email", email),
        ("phone", phone),
        ("account_id", account_id),
    ):
        if value is not _UNSET:
            params.append(value)
            set_clauses.append(f"{column} = ${len(params)}")

    if not set_clauses:
        # Nothing to update; just return the current row (tenant-scoped read).
        return await get_contact(db, claims, contact_id)

    params.append(claims.tenant_id)
    tenant_idx = len(params)
    params.append(contact_id)
    contact_idx = len(params)

    # Parameterized SQL; set_clauses built from a fixed, safe column allowlist.
    # ruff: noqa: S608
    result = await db.execute(
        "UPDATE contacts SET " + ", ".join(set_clauses) + ", updated_at = now() "
        f"WHERE tenant_id = ${tenant_idx} AND contact_id = ${contact_idx}",
        *params,
    )
    if _rows_affected(result) == 0:
        return None

    return await get_contact(db, claims, contact_id)


async def add_identity(
    db: Database,
    claims: AuthClaims,
    contact_id: str,
    *,
    identity_type: str,
    identity_value: str,
) -> str:
    """Insert a new ``contact_identities`` row. Returns the ``identity_id``.

    Raises ``ValidationError`` (``IDENTITY_ALREADY_CLAIMED``) if this
    ``(tenant_id, identity_type, identity_value)`` triple already belongs to
    a different contact (D4's tenant-scoped unique index).
    """
    _reject_global(claims)

    new_identity_id = uuid4().hex
    try:
        await db.execute(
            "INSERT INTO contact_identities "
            "(tenant_id, identity_id, contact_id, identity_type, identity_value) "
            "VALUES ($1, $2, $3, $4, $5)",
            claims.tenant_id,
            new_identity_id,
            contact_id,
            identity_type,
            identity_value,
        )
    except UniqueViolationError as exc:
        raise ValidationError(
            "This identity is already claimed by another contact in this tenant.",
            code="IDENTITY_ALREADY_CLAIMED",
        ) from exc
    return new_identity_id


async def list_identities(
    db: Database,
    claims: AuthClaims,
    contact_id: str,
) -> list[ContactIdentity]:
    """Fetch a contact's identities, tenant-scoped, newest first."""
    _reject_global(claims)

    rows = await db.fetch(
        "SELECT identity_id, contact_id, identity_type, identity_value, created_at "
        "FROM contact_identities "
        "WHERE tenant_id = $1 AND contact_id = $2 "
        "ORDER BY created_at DESC",
        claims.tenant_id,
        contact_id,
    )
    return [_row_to_identity(row) for row in rows]


async def get_contact_id_by_identity(
    db: Database,
    claims: AuthClaims,
    *,
    identity_type: str,
    identity_value: str,
) -> str | None:
    """Resolve a contact_id from an identity value, tenant-scoped, or ``None``."""
    _reject_global(claims)

    row = await db.fetchrow(
        "SELECT contact_id FROM contact_identities "
        "WHERE tenant_id = $1 AND identity_type = $2 AND identity_value = $3",
        claims.tenant_id,
        identity_type,
        identity_value,
    )
    return str(row["contact_id"]) if row is not None else None


def _rows_affected(command_tag: str) -> int:
    """Parse the row count from an asyncpg-style command tag (e.g. 'UPDATE 1')."""
    parts = command_tag.strip().split()
    if not parts:
        return 0
    try:
        return int(parts[-1])
    except ValueError:
        return 0


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _row_to_contact(row: Any) -> Contact:
    return Contact(
        contact_id=str(row["contact_id"]),
        account_id=row["account_id"],
        lead_id=row["lead_id"],
        name=row["name"],
        email=row["email"],
        phone=row["phone"],
        consent=row["consent"],
        owner_agent_id=row["owner_agent_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_identity(row: Any) -> ContactIdentity:
    return ContactIdentity(
        identity_id=str(row["identity_id"]),
        contact_id=str(row["contact_id"]),
        identity_type=str(row["identity_type"]),
        identity_value=str(row["identity_value"]),
        created_at=row["created_at"],
    )

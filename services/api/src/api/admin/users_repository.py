"""Account user-management repository (S12.2, re-scoped for multi-chatbot
accounts) -- list/invite/deactivate an ACCOUNT's ``CLIENT_AGENT``s.

Unlike ``api.admin.repository`` (S12.1, deliberately global -- there is no
tenant yet at onboarding time), every function here is strictly own-ACCOUNT
-scoped: membership is account-wide (an agent can switch across every
chatbot in the account, matching the multi-chatbot-accounts feature), never
tied to whichever tenant happens to be active in ``claims.tenant_id`` at
call time -- see ``_require_account_scoped_client_admin``'s docstring for
exactly how the account is resolved and why. A ``PLATFORM_ADMIN``/global
caller is rejected with ``ValidationError GLOBAL_CALLER_NOT_PERMITTED``
(mirrors ``api.orchestrator.config_repository._reject_global``); any other
non-``CLIENT_ADMIN`` role is rejected with ``AuthorizationError
ROLE_NOT_PERMITTED`` via ``common.tenancy.require_role`` (defense in depth --
the route layer already gates on ``Role.CLIENT_ADMIN``).

``create_tenant_agent`` hardcodes ``role='CLIENT_AGENT'`` in the bound INSERT
param -- there is no ``role`` field on the request body to even attempt
overriding (decision 1). ``set_user_active`` enforces the "manage agents, not
peers" symmetry (decisions 3-4): the target must be a same-account
``CLIENT_AGENT`` and must not be the caller themselves, else ``ValidationError
INVALID_TARGET_USER``; a missing/cross-account target returns ``None`` (route
maps to 404), never leaking which case it was.
"""
from __future__ import annotations

import secrets
from typing import Any
from uuid import uuid4

import asyncpg
from common.auth import AuthClaims, Role
from common.crypto import hash_password
from common.db import Database
from common.errors import NotFoundError, ValidationError
from common.tenancy import require_role

from api.auth.repository import get_tenant_for_switch

_GENERATED_PASSWORD_BYTES = 16


async def _require_account_scoped_client_admin(db: Database, claims: AuthClaims) -> str:
    """Reject a global (PLATFORM_ADMIN) caller, require Role.CLIENT_ADMIN, and
    resolve the ACCOUNT that ``claims.tenant_id`` belongs to -- the real
    scoping key now that agent membership is account-wide (a member can
    switch across every chatbot in the account), not tied to one tenant.

    Resolves via the TARGET tenant's ``client_account_id`` (``get_tenant_for_
    switch``), deliberately NOT via the caller's own user row: this
    function is reachable both from a real CLIENT_ADMIN's own implicit
    routes (``claims.tenant_id`` = their own active tenant, ``claims.subject``
    = their own user id) AND from a PLATFORM_ADMIN's ``resolve_tenant_scope``-
    derived claims on the tenant-explicit ``/admin/tenants/{tenant_id}/users``
    routes (``claims.tenant_id`` = the validated TARGET tenant, but
    ``claims.subject`` is the REAL platform admin's own id, not any member of
    that tenant's account). Resolving by ``claims.subject`` would look up the
    platform admin's own accountless row in the second case; resolving by
    ``claims.tenant_id`` gives the right account in both.

    PLATFORM_ADMIN callers always have ``tenant_id is None`` (see
    ``common.auth.AuthClaims.__post_init__``), so the global check always
    catches a REAL platform-admin-as-itself request first with the
    decision-2-mandated ``GLOBAL_CALLER_NOT_PERMITTED`` code -- this only
    matters for the *derived* claims a resolve_tenant_scope request carries,
    which have ``role=CLIENT_ADMIN`` and a real ``tenant_id``. Any other
    non-CLIENT_ADMIN role (CLIENT_AGENT, VISITOR) is rejected by
    ``require_role``.
    """
    if claims.tenant_id is None:
        raise ValidationError(
            "User management is account-scoped; PLATFORM_ADMIN callers are "
            "not permitted.",
            code="GLOBAL_CALLER_NOT_PERMITTED",
        )
    require_role(claims, Role.CLIENT_ADMIN)

    tenant_row = await get_tenant_for_switch(db, claims.tenant_id)
    account_id = tenant_row.get("client_account_id") if tenant_row is not None else None
    if account_id is None:
        raise NotFoundError("Account not found.", code="ACCOUNT_NOT_FOUND")
    return str(account_id)


async def list_tenant_users(db: Database, claims: AuthClaims) -> list[dict[str, Any]]:
    """List the caller's ACCOUNT's users, newest first -- account-scoped
    (membership is account-wide since an agent can switch across every
    chatbot in the account). Never includes ``password_hash``."""
    account_id = await _require_account_scoped_client_admin(db, claims)

    rows = await db.fetch(
        "SELECT id, tenant_id, client_account_id, email, role, name, active, "
        "last_login_at, created_at "
        "FROM users WHERE client_account_id = $1 ORDER BY created_at DESC",
        account_id,
    )
    return [dict(row) for row in rows]


async def create_tenant_agent(
    db: Database,
    claims: AuthClaims,
    *,
    email: str,
    name: str | None,
) -> dict[str, Any]:
    """Create a new ``CLIENT_AGENT`` in the caller's ACCOUNT -- switchable
    across every chatbot in it, not tied to the tenant active at invite time.

    ``role='CLIENT_AGENT'`` is hardcoded -- there is no request-body ``role``
    field to even attempt to override (decision 1). Generates and hashes a
    fresh temp password (``secrets.token_urlsafe``, mirrors S12.1's generated-
    password pattern); the raw value is returned exactly once, never
    persisted anywhere but its hash. A duplicate email (case-insensitive,
    ``users_email_lower_uniq``) raises ``ValidationError ADMIN_EMAIL_TAKEN``.

    ``tenant_id`` is still bound on the INSERT (the caller's currently
    active tenant) ONLY to satisfy the legacy ``users_tenant_role_chk`` CHECK
    constraint kept during the expand-only migration window -- it is
    vestigial from here on and never read for any authorization decision;
    ``client_account_id`` is the real scoping column.
    """
    account_id = await _require_account_scoped_client_admin(db, claims)

    raw_password = secrets.token_urlsafe(_GENERATED_PASSWORD_BYTES)
    password_hash = hash_password(raw_password)

    user_id = uuid4().hex
    try:
        await db.execute(
            "INSERT INTO users (id, tenant_id, client_account_id, email, role, "
            "password_hash, name) VALUES ($1, $2, $3, $4, $5, $6, $7)",
            user_id,
            claims.tenant_id,
            account_id,
            email,
            Role.CLIENT_AGENT.value,
            password_hash,
            name,
        )
    except asyncpg.UniqueViolationError as exc:
        raise ValidationError(
            "A user with this email already exists.",
            code="ADMIN_EMAIL_TAKEN",
        ) from exc

    return {
        "user_id": user_id,
        "tenant_id": claims.tenant_id,
        "email": email,
        "role": Role.CLIENT_AGENT.value,
        "name": name,
        "active": True,
        "temp_password": raw_password,
    }


async def set_user_active(
    db: Database,
    claims: AuthClaims,
    user_id: str,
    *,
    active: bool,
) -> dict[str, Any] | None:
    """Set ``users.active`` for a same-ACCOUNT ``CLIENT_AGENT`` target.

    Returns ``None`` for a missing or cross-account ``user_id`` (route maps
    to 404 ``USER_NOT_FOUND`` -- indistinguishable from "doesn't exist",
    decision 2). Raises ``ValidationError INVALID_TARGET_USER`` when the
    target is the caller themselves (decision 4) or is not a ``CLIENT_AGENT``
    in the caller's account (decision 3) -- the row exists and is visible via
    ``list_tenant_users``, it is just not a legal PATCH target.
    """
    account_id = await _require_account_scoped_client_admin(db, claims)

    row = await db.fetchrow(
        "SELECT id, tenant_id, client_account_id, email, role, name, active, "
        "last_login_at, created_at "
        "FROM users WHERE id = $1 AND client_account_id = $2",
        user_id,
        account_id,
    )
    if row is None:
        return None

    if user_id == claims.subject or row["role"] != Role.CLIENT_AGENT.value:
        raise ValidationError(
            "This user is not a legal target for activation/deactivation.",
            code="INVALID_TARGET_USER",
        )

    updated = await db.fetchrow(
        "UPDATE users SET active = $1, updated_at = now() "
        "WHERE id = $2 AND client_account_id = $3 "
        "RETURNING id, tenant_id, client_account_id, email, role, name, active, "
        "last_login_at, created_at",
        active,
        user_id,
        account_id,
    )
    return dict(updated) if updated is not None else None


async def delete_tenant_agent(
    db: Database, claims: AuthClaims, user_id: str
) -> dict[str, Any] | None:
    """Permanently delete a same-ACCOUNT ``CLIENT_AGENT`` target -- ONLY if
    already inactive.

    Deletion is restricted to already-deactivated members (user request: an
    active login is never destroyed by one click -- the caller must
    Deactivate first, an existing, separately-confirmed step). Returns
    ``None`` for a missing or cross-account ``user_id`` (route maps to 404
    ``USER_NOT_FOUND`` -- same indistinguishable-from-nonexistent handling as
    ``set_user_active``). Raises ``ValidationError INVALID_TARGET_USER`` when
    the target is the caller themselves or is not a ``CLIENT_AGENT`` in the
    caller's account (same symmetry as ``set_user_active``'s decisions 3-4).
    Raises ``ValidationError USER_NOT_INACTIVE`` when the target is still
    active.

    Safe as a hard DELETE: no FK constraint anywhere in the schema
    references ``users(id)`` -- every other table's user-referencing column
    (``leads.assigned_agent_id``, notification recipients, audit actor ids)
    is a plain, unconstrained text column, so this can never violate a
    foreign key or be blocked by one (unlike a tenant delete).
    """
    account_id = await _require_account_scoped_client_admin(db, claims)

    row = await db.fetchrow(
        "SELECT id, tenant_id, client_account_id, email, role, name, active, "
        "last_login_at, created_at "
        "FROM users WHERE id = $1 AND client_account_id = $2",
        user_id,
        account_id,
    )
    if row is None:
        return None

    if user_id == claims.subject or row["role"] != Role.CLIENT_AGENT.value:
        raise ValidationError(
            "This user is not a legal target for deletion.",
            code="INVALID_TARGET_USER",
        )

    if row["active"]:
        raise ValidationError(
            "Deactivate this member before deleting them.",
            code="USER_NOT_INACTIVE",
        )

    deleted = await db.fetchrow(
        "DELETE FROM users WHERE id = $1 AND client_account_id = $2 "
        "RETURNING id, tenant_id, client_account_id, email, role, name, active, "
        "last_login_at, created_at",
        user_id,
        account_id,
    )
    return dict(deleted) if deleted is not None else None

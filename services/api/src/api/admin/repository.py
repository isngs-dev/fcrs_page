"""Admin repository -- one-shot tenant onboarding + client-key rotation (S12.1).

Both operations are PLATFORM_ADMIN-only and deliberately global (no
``tenant_id`` filter -- there is no tenant yet at onboarding time, and
rotation targets an arbitrary tenant by id), mirroring
``api.tenants.repository.TenantRepository.create``'s existing precedent.

``_hash_client_key`` is the shared SHA-256 helper ported from
``api.auth.password_reset._hash_token`` (decision 3): a public,
high-QPS, non-brute-force-target lookup key gets a plain SHA-256 hash +
unique-index lookup, not the deliberately slow PBKDF2 password path.
``api.gateway.repository`` imports this helper (admin/ creates keys,
gateway/ only validates them -- no circular/duplicated helper).

Sequential inserts, no transaction wrapper (decision 2 -- matches this
codebase's established ``Database`` surface, e.g.
``api.leads.repository``'s stage-transition + activity-log flow): the
tenant row and its hashed client key are committed before the admin user
insert is attempted. A duplicate ``admin_email`` therefore leaves a
tenant-without-admin-user row behind -- disclosed, not auto-rolled-back
(Open question 1).
"""
from __future__ import annotations

import hashlib
import re
import secrets
from typing import Any
from uuid import uuid4

import asyncpg
from common.auth import AuthClaims, Role
from common.crypto import hash_password
from common.db import Database
from common.errors import AuthorizationError, NotFoundError, ValidationError
from common.tenancy import require_role

from api.auth.repository import get_user_by_id

_CLIENT_KEY_PREFIX = "pk_"  # noqa: S105
_CLIENT_KEY_RANDOM_BYTES = 24
_GENERATED_PASSWORD_BYTES = 16

_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


def _hash_client_key(raw: str) -> str:
    """SHA-256 hex digest of a raw client key (mirrors ``password_reset._hash_token``)."""
    return hashlib.sha256(raw.encode()).hexdigest()


def _is_client_key_hash(value: str) -> bool:
    """True if ``value`` already looks like a ``_hash_client_key`` output.

    A SHA-256 hex digest is always exactly 64 lowercase hex characters.
    Used by migration 0032 (backfill for the 0030 gap) to distinguish
    already-hashed ``tenants.client_key_hash`` values (tenants created
    after 0030 via ``_hash_client_key``, e.g. through
    ``create_tenant_with_admin`` or ``rotate_client_key``) from still-
    plaintext values left over from tenants that existed before 0030 ran
    (0030 renamed the column but did not backfill/rehash existing rows).
    Deliberately strict (lowercase-only, exact length) so a second
    migration run never double-hashes an already-hashed value.
    """
    return bool(_SHA256_HEX_RE.fullmatch(value))


def _generate_client_key() -> str:
    return f"{_CLIENT_KEY_PREFIX}{secrets.token_urlsafe(_CLIENT_KEY_RANDOM_BYTES)}"


async def create_tenant_with_admin(
    db: Database,
    claims: AuthClaims,
    *,
    name: str,
    slug: str,
    admin_email: str,
    admin_password: str | None,
    admin_name: str | None,
) -> dict[str, Any]:
    """Create a tenant + hashed client key + first CLIENT_ADMIN user.

    Requires ``Role.PLATFORM_ADMIN`` -- checked BEFORE any insert. Sequential
    inserts (decision 2):

    1. Insert the tenant row. A ``slug`` collision raises ``ValidationError``
       ``TENANT_SLUG_TAKEN`` before anything else has run.
    2. Generate + hash a fresh client key, ``UPDATE tenants SET
       client_key_hash = $1``.
    3. Hash the admin password (caller-supplied, or a fresh
       ``secrets.token_urlsafe(16)`` when omitted) and insert the first
       ``CLIENT_ADMIN`` user. An ``admin_email`` collision (case-insensitive,
       ``users_email_lower_uniq``) raises ``ValidationError``
       ``ADMIN_EMAIL_TAKEN`` -- the tenant row and its hashed client key
       already exist at this point and are NOT rolled back.

    Returns ``{tenant_id, name, slug, client_key, admin_user_id, admin_email,
    password_was_generated}``. ``client_key`` is the raw (pre-hash) value --
    it is never persisted anywhere, only its hash is. The route layer decides
    whether to echo ``admin_password`` back based on
    ``password_was_generated``.
    """
    require_role(claims, Role.PLATFORM_ADMIN)

    tenant_id = uuid4().hex
    try:
        await db.execute(
            "INSERT INTO tenants (id, name, slug, enabled) VALUES ($1, $2, $3, $4)",
            tenant_id,
            name,
            slug,
            True,
        )
    except asyncpg.UniqueViolationError as exc:
        raise ValidationError(
            "A tenant with this slug already exists.",
            code="TENANT_SLUG_TAKEN",
        ) from exc

    raw_client_key = _generate_client_key()
    client_key_hash = _hash_client_key(raw_client_key)
    await db.execute(
        "UPDATE tenants SET client_key_hash = $1 WHERE id = $2",
        client_key_hash,
        tenant_id,
    )

    password_was_generated = admin_password is None
    raw_password = (
        secrets.token_urlsafe(_GENERATED_PASSWORD_BYTES)
        if password_was_generated
        else admin_password
    )
    assert raw_password is not None  # narrowed above  # noqa: S101
    password_hash = hash_password(raw_password)

    admin_user_id = uuid4().hex
    try:
        await db.execute(
            "INSERT INTO users (id, tenant_id, email, role, password_hash, name) "
            "VALUES ($1, $2, $3, $4, $5, $6)",
            admin_user_id,
            tenant_id,
            admin_email,
            Role.CLIENT_ADMIN.value,
            password_hash,
            admin_name,
        )
    except asyncpg.UniqueViolationError as exc:
        raise ValidationError(
            "A user with this email already exists.",
            code="ADMIN_EMAIL_TAKEN",
        ) from exc

    return {
        "tenant_id": tenant_id,
        "name": name,
        "slug": slug,
        "client_key": raw_client_key,
        "admin_user_id": admin_user_id,
        "admin_email": admin_email,
        "password_was_generated": password_was_generated,
        "admin_password": raw_password if password_was_generated else None,
    }


async def _rotate_client_key_impl(db: Database, tenant_id: str) -> str | None:
    """The shared crypto/SQL logic behind BOTH ``rotate_client_key``
    (PLATFORM_ADMIN, any tenant) and ``rotate_own_client_key`` (SR-20 D6:
    CLIENT_ADMIN, own tenant only). No RBAC check -- callers gate first.

    Mints + hashes a fresh client key for ``tenant_id``; the old key stops
    working immediately (no grace-period dual-validity -- decision 8).
    Returns the new RAW key on success, or ``None`` if ``tenant_id`` does
    not exist.
    """
    raw_client_key = _generate_client_key()
    client_key_hash = _hash_client_key(raw_client_key)
    row = await db.fetchrow(
        "UPDATE tenants SET client_key_hash = $1 WHERE id = $2 RETURNING id",
        client_key_hash,
        tenant_id,
    )
    if row is None:
        return None
    return raw_client_key


async def rotate_client_key(
    db: Database, claims: AuthClaims, tenant_id: str
) -> str | None:
    """Mint + hash a fresh client key for ``tenant_id``; the old key stops working
    immediately (no grace-period dual-validity -- decision 8).

    Requires ``Role.PLATFORM_ADMIN`` -- UNCHANGED since S12.1/M7. This is the
    tenant-explicit route's function; ``CLIENT_ADMIN`` self-service rotation
    is a SEPARATE function, ``rotate_own_client_key`` (SR-20 D6), so this
    route's existing RBAC and tests are untouched. Returns the new RAW key
    on success, or ``None`` if ``tenant_id`` does not exist (route maps this
    to 404 ``TENANT_NOT_FOUND``).
    """
    require_role(claims, Role.PLATFORM_ADMIN)
    return await _rotate_client_key_impl(db, tenant_id)


async def rotate_own_client_key(db: Database, claims: AuthClaims) -> str | None:
    """Mint + hash a fresh client key for the CALLER'S OWN tenant (SR-20 D6).

    Requires ``Role.CLIENT_ADMIN`` with a tenant_id (never a global caller).
    Unlike ``rotate_client_key``, the target tenant is ALWAYS
    ``claims.tenant_id`` -- never a caller-supplied id -- so a CLIENT_ADMIN
    can only ever rotate their own tenant's key (CLAUDE.md §3: tenant_id
    never from input). Reuses the exact same crypto/SQL as the existing
    PLATFORM_ADMIN path (``_rotate_client_key_impl``) -- one rotation
    mechanism, two RBAC-gated entry points, per M6/D6 ("extend, never
    reinvent").

    Returns the new RAW key on success. Raises ``AuthorizationError`` for
    any other role. ``tenant_id`` always exists for a CLIENT_ADMIN (the
    tenant row IS what makes the caller a CLIENT_ADMIN), so this never
    returns ``None`` -- unlike the PLATFORM_ADMIN path, there is no
    "unknown tenant" case to handle here.
    """
    if claims.role != Role.CLIENT_ADMIN or claims.tenant_id is None:
        raise AuthorizationError(
            "Only a CLIENT_ADMIN may rotate their own tenant's client key.",
            code="ROLE_NOT_PERMITTED",
        )
    return await _rotate_client_key_impl(db, claims.tenant_id)


async def _resolve_own_account_id(db: Database, claims: AuthClaims) -> str:
    """The caller's own ``client_account_id``, resolved from their user row --
    authoritative, and NOT derived from whichever tenant happens to be
    active in ``claims.tenant_id`` (account membership, not the
    currently-switched-to chatbot, is what governs multi-chatbot access).
    """
    user_row = await get_user_by_id(db, claims.subject)
    account_id = user_row.get("client_account_id") if user_row is not None else None
    if account_id is None:
        raise NotFoundError("Account not found.", code="ACCOUNT_NOT_FOUND")
    return str(account_id)


async def create_tenant_for_own_account(
    db: Database,
    claims: AuthClaims,
    *,
    name: str,
    slug: str,
) -> dict[str, Any]:
    """Create a new chatbot (tenant) under the CALLER'S OWN account --
    CLIENT_ADMIN self-service for multi-chatbot accounts.

    Unlike ``create_tenant_with_admin``, this creates NO new user: the
    caller's own account membership already grants every member of the
    account access to the new chatbot via ``POST /auth/switch-tenant``.
    Reuses the exact same client-key generation/hashing helpers as the
    platform onboarding path (one mechanism, two RBAC-gated entry points,
    per the same "extend, never reinvent" precedent as
    ``rotate_own_client_key``).

    Requires ``Role.CLIENT_ADMIN`` -- checked BEFORE any insert. No cap on
    how many chatbots one account may create (accepted gap: no billing/
    plan-tier system exists yet). Same ``TENANT_SLUG_TAKEN`` handling as
    today's platform onboarding.
    """
    if claims.role != Role.CLIENT_ADMIN or claims.tenant_id is None:
        raise AuthorizationError(
            "Only a CLIENT_ADMIN may create a chatbot for their own account.",
            code="ROLE_NOT_PERMITTED",
        )

    account_id = await _resolve_own_account_id(db, claims)

    tenant_id = uuid4().hex
    try:
        await db.execute(
            "INSERT INTO tenants (id, name, slug, enabled, client_account_id) "
            "VALUES ($1, $2, $3, $4, $5)",
            tenant_id,
            name,
            slug,
            True,
            account_id,
        )
    except asyncpg.UniqueViolationError as exc:
        raise ValidationError(
            "A tenant with this slug already exists.",
            code="TENANT_SLUG_TAKEN",
        ) from exc

    raw_client_key = _generate_client_key()
    client_key_hash = _hash_client_key(raw_client_key)
    await db.execute(
        "UPDATE tenants SET client_key_hash = $1 WHERE id = $2",
        client_key_hash,
        tenant_id,
    )

    return {
        "tenant_id": tenant_id,
        "name": name,
        "slug": slug,
        "client_key": raw_client_key,
    }


# Tables with a plain (unconstrained-to-tenants) ``tenant_id`` column -- no
# ``ON DELETE CASCADE`` exists for any of these, so a hard tenant delete must
# clear them explicitly. Order matters for exactly one edge --
# ``opportunities.contact_id -> contacts`` has no ``ON DELETE`` clause
# (defaults to RESTRICT), so ``opportunities`` must be cleared before
# ``contacts``. Every other edge here is CASCADE/SET NULL (order is then
# just hygiene, not a requirement) -- full audit in the delete-chatbot plan.
_DEPENDENT_TABLES_DELETE_ORDER: tuple[str, ...] = (
    "contact_identities",
    "opportunities",
    "lead_activities",
    "reminder_jobs",
    "notification_event_reads",
    "contacts",
    "accounts",
    "leads",
    "schedule_events",
    "notification_events",
    "tenant_crm_configs",
    "audit_events",
    "availability",
    "tenant_calendar_configs",
    "tenant_notification_configs",
    "notification_jobs",
    "tenant_opportunity_configs",
    "tenant_call_configs",
)


async def delete_own_tenant(db: Database, claims: AuthClaims) -> dict[str, Any]:
    """Permanently (hard) delete the CALLER'S OWN currently-active chatbot.

    Requires ``Role.CLIENT_ADMIN``; the target is ALWAYS ``claims.tenant_id``,
    never a caller-supplied id (same own-tenant-only shape as
    ``rotate_own_client_key``).

    Refuses (``ValidationError`` ``LAST_CHATBOT``) if this is the account's
    only remaining ENABLED chatbot -- deleting it would leave the account
    unable to log in at all (``NO_ACCESSIBLE_TENANT``). The check runs as the
    first statement inside the delete transaction, after taking a
    per-account advisory lock (``pg_advisory_xact_lock``), so two concurrent
    deletes against the same account's last two chatbots can't both pass it.

    Everything else runs in one transaction on one connection
    (``db.acquire()`` + ``conn.transaction()`` -- a deliberate exception to
    this file's usual "sequential inserts, no transaction wrapper"
    convention: an interrupted hard delete leaves cross-table data
    corruption, not just a disclosed leftover row):

    1. Resolve the account's next tenant (earliest-created other ENABLED
       tenant) -- this is both the last-chatbot guard and the id the
       session gets re-minted onto.
    2. Repoint every user's legacy ``tenant_id`` off the tenant being
       deleted onto that next tenant. ``users.tenant_id`` still carries
       ``ON DELETE CASCADE`` to ``tenants(id)`` (kept, unused, for the
       multi-chatbot-accounts expand-only migration window) -- without this
       step, deleting the tenant would cascade-delete the login of every
       user whose vestigial ``tenant_id`` still points at it, which
       includes the caller themselves whenever they weren't invited under
       this specific chatbot.
    3. Clear every dependent table with no FK to ``tenants``
       (``_DEPENDENT_TABLES_DELETE_ORDER``).
    4. ``DELETE FROM tenants`` -- now safe to cascade (users, conversations,
       messages, knowledge_docs, ingestion_runs, and the per-tenant config
       tables that already have real ``ON DELETE CASCADE`` constraints).

    Returns ``{deleted_tenant_id, next_tenant_id}``. The route mints the new
    session cookie onto ``next_tenant_id`` -- this function does no
    cookie/response work, matching how ``switch_tenant``'s route keeps that
    separate from its own DB lookups.
    """
    if claims.role != Role.CLIENT_ADMIN or claims.tenant_id is None:
        raise AuthorizationError(
            "Only a CLIENT_ADMIN may delete their own tenant's chatbot.",
            code="ROLE_NOT_PERMITTED",
        )

    tenant_id = claims.tenant_id
    account_id = await _resolve_own_account_id(db, claims)

    async with db.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", account_id)

            next_row = await conn.fetchrow(
                "SELECT id FROM tenants WHERE client_account_id = $1 AND enabled "
                "AND id != $2 ORDER BY created_at ASC LIMIT 1",
                account_id,
                tenant_id,
            )
            if next_row is None:
                raise ValidationError(
                    "This is the only chatbot left in your account -- delete "
                    "another chatbot instead, or contact support.",
                    code="LAST_CHATBOT",
                )
            next_tenant_id = str(next_row["id"])

            await conn.execute(
                "UPDATE users SET tenant_id = $1 WHERE tenant_id = $2",
                next_tenant_id,
                tenant_id,
            )

            for table in _DEPENDENT_TABLES_DELETE_ORDER:
                await conn.execute(f"DELETE FROM {table} WHERE tenant_id = $1", tenant_id)  # noqa: S608

            await conn.execute("DELETE FROM tenants WHERE id = $1", tenant_id)

    return {"deleted_tenant_id": tenant_id, "next_tenant_id": next_tenant_id}


async def list_tenants_for_own_account(db: Database, claims: AuthClaims) -> list[dict[str, Any]]:
    """List every chatbot (tenant) in the CALLER'S OWN account.

    ``Role.CLIENT_ADMIN`` or ``Role.CLIENT_AGENT`` -- both get the same
    account-level "my chatbots" visibility (the switcher is symmetric;
    per-endpoint role gates elsewhere already restrict what an agent can
    DO once switched, so no new permission logic is needed here).
    """
    if claims.role not in (Role.CLIENT_ADMIN, Role.CLIENT_AGENT) or claims.tenant_id is None:
        raise AuthorizationError(
            "Only an account member may list their own chatbots.",
            code="ROLE_NOT_PERMITTED",
        )

    account_id = await _resolve_own_account_id(db, claims)
    rows = await db.fetch(
        "SELECT id, name, slug, enabled, created_at FROM tenants "
        "WHERE client_account_id = $1 ORDER BY created_at",
        account_id,
    )
    return [dict(r) for r in rows]

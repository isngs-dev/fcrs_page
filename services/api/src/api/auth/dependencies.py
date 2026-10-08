"""Auth dependencies -- resolve AuthClaims from the request cookie.

``get_current_claims`` reads the JWT from the httpOnly cookie, validates it,
checks the Redis jti-blacklist (S1.4), and returns ``AuthClaims``.
``require_roles`` is a dependency factory that additionally enforces RBAC.
``resolve_tenant_scope`` (S12.7) is the platform-admin super-user seam --
see its docstring below.
"""
from __future__ import annotations

from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from common.auth import AuthClaims, Role
from common.errors import AuthenticationError, NotFoundError
from common.logging import get_logger
from common.tenancy import require_role
from fastapi import Depends, Request

from api.auth.blacklist import get_token_blacklist
from api.auth.tokens import claims_from_payload, decode_access_token
from api.config import get_api_settings

_log = get_logger(__name__)


async def get_current_claims(request: Request) -> AuthClaims:
    """Extract and validate AuthClaims from the access-token cookie.

    Raises ``AuthenticationError`` (401) when the cookie is missing, the token
    is invalid, the token has expired, or the token's jti is blacklisted
    (logged out). The blacklist check is fail-closed: a Redis error denies
    the request.
    """
    settings = get_api_settings()
    token: str | None = request.cookies.get(settings.cookie_name)
    if token is None:
        # TEMP diagnostic (SR-22 UNAUTHENTICATED-on-Google-callback investigation,
        # 2026-09-24) -- never logs cookie/token VALUES, only whether a Cookie
        # header arrived at all and which cookie NAMES Starlette parsed from it,
        # to tell apart "browser never sent it" from "something between the
        # browser and this handler dropped/renamed it". Remove once resolved.
        _log.warning(
            "no access_token cookie on request",
            extra={
                "event": "missing_auth_cookie",
                "path": request.url.path,
                "has_cookie_header": request.headers.get("cookie") is not None,
                "parsed_cookie_names": sorted(request.cookies.keys()),
            },
        )
        raise AuthenticationError("Authentication is required.")

    payload = decode_access_token(token, secret=settings.jwt_secret)

    # Redis jti-blacklist check (S1.4). Fail-closed: RedisTokenBlacklist
    # raises AuthenticationError on RedisError.
    jti = payload.get("jti")
    if jti and await get_token_blacklist(request).is_revoked(str(jti)):
        raise AuthenticationError("Session has been revoked.")

    return claims_from_payload(payload)


def require_roles(
    *roles: Role,
) -> Callable[..., Coroutine[Any, Any, AuthClaims]]:
    """Dependency factory: resolve claims then enforce RBAC.

    Usage::

        @router.get("/admin-only")
        async def admin_only(
            claims: AuthClaims = Depends(require_roles(Role.PLATFORM_ADMIN)),
        ) -> ...:
    """

    async def _dependency(
        claims: AuthClaims = Depends(get_current_claims),  # noqa: B008
    ) -> AuthClaims:
        require_role(claims, *roles)
        return claims

    return _dependency


@dataclass(frozen=True)
class PlatformAdminActor:
    """The REAL identity of a platform admin acting on a derived, tenant-scoped
    ``AuthClaims``. ``AuthClaims`` itself stays unchanged (D7) -- this thin
    wrapper carries the true actor for ``record_audit`` to read, alongside the
    effective, data-access-scoped claims.

    ``resolve_tenant_scope`` attaches this on ``request.state.platform_admin_actor``
    when (and only when) a PLATFORM_ADMIN reached a tenant-explicit route. It is
    absent for a real CLIENT_ADMIN/CLIENT_AGENT request (nothing to record).
    """

    subject: str
    role: Role


def get_platform_admin_actor(request: Request) -> PlatformAdminActor | None:
    """Read the real platform-admin actor stashed by ``resolve_tenant_scope``,
    or ``None`` for a normal (non-platform-admin, non-tenant-explicit) request.

    Handlers pass this straight through to ``record_audit(..., actor_context=...)``
    (D4) -- no hand-assembly of metadata at each call site.
    """
    actor = getattr(request.state, "platform_admin_actor", None)
    if isinstance(actor, PlatformAdminActor):
        return actor
    return None


def resolve_tenant_scope(
    *roles: Role,
) -> Callable[..., Coroutine[Any, Any, AuthClaims]]:
    """Dependency factory for the ``/admin/tenants/{tenant_id}/...`` platform-
    explicit route family (S12.7 D2/D3).

    ``roles`` is accepted for call-site symmetry with ``require_roles`` (the
    locked D2 signature) but is currently unused: this route family is
    PLATFORM_ADMIN-only by construction (D3) regardless of ``roles``. Real
    ``CLIENT_ADMIN``/``CLIENT_AGENT`` callers never reach a tenant-explicit
    route at all -- they keep using their existing implicit
    ``require_roles(...)`` routes, where the tenant comes from their own
    ``claims.tenant_id`` and no target ``tenant_id`` is ever read.

    Resolves the ``AuthClaims`` a tenant-scoped handler should run under:

    - A ``PLATFORM_ADMIN``: the target ``tenant_id`` is read from the path,
      validated to exist and be active via the tenants repository (404
      ``TENANT_NOT_FOUND`` otherwise -- no silent scoped-to-nothing session),
      and a DERIVED, request-scoped ``AuthClaims`` is returned:
      ``AuthClaims(subject=<real platform admin subject>, role=CLIENT_ADMIN,
      tenant_id=<validated target X>)``. The true actor identity (real
      subject + real PLATFORM_ADMIN role) is attached to
      ``request.state.platform_admin_actor`` for ``record_audit`` (D4).
    - Any other role (``CLIENT_ADMIN``/``CLIENT_AGENT``/``VISITOR``) ->
      ``AuthorizationError(ROLE_NOT_PERMITTED)`` (403), BEFORE the
      ``{tenant_id}`` path segment is ever honored. This route family is
      PLATFORM_ADMIN-only by construction (D3) -- a client role has no route
      into cross-tenant reach here, not a check that could be fooled: their
      own implicit routes (``require_roles``) are the only path they ever
      use, and those never read a target ``tenant_id`` at all.
    - No cookie / invalid token -> ``AuthenticationError`` (401), via
      ``get_current_claims``.
    - Unknown / inactive target tenant -> ``NotFoundError`` (404), never a
      scoped-to-nothing session (no silent fallback, CLAUDE.md §3).
    """

    async def _dependency(
        request: Request,
        tenant_id: str,
        claims: AuthClaims = Depends(get_current_claims),  # noqa: B008
    ) -> AuthClaims:
        require_role(claims, Role.PLATFORM_ADMIN)

        db = request.app.state.db
        from api.tenants.repository import TenantRepository  # noqa: PLC0415

        tenant_repo = TenantRepository(db)
        tenant_row = await tenant_repo.get(claims, tenant_id)
        if tenant_row is None or not tenant_row.get("enabled", False):
            raise NotFoundError("Tenant not found.", code="TENANT_NOT_FOUND")

        request.state.platform_admin_actor = PlatformAdminActor(
            subject=claims.subject, role=claims.role,
        )
        return AuthClaims(
            subject=claims.subject,
            role=Role.CLIENT_ADMIN,
            tenant_id=tenant_id,
        )

    return _dependency

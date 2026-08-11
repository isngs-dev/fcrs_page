"""API-keys repository (SR-20 D6) -- CLIENT_ADMIN self-service client-key
rotation + Origin allowlist read/update.

Extends the EXISTING ``pk_`` client-key mechanism (M6:
``api.admin.repository._CLIENT_KEY_PREFIX``, ``_hash_client_key``,
``rotate_own_client_key``) -- no parallel key system, no per-user tokens.
The stored value is a SHA-256 hash and cannot be un-hashed (M6), so
``get_api_key_info`` NEVER returns the raw or hashed key -- only whether a
key exists (``has_key``) and the Origin allowlist. The raw key is exposed
exactly once, by ``rotate_own_key``, in the mutation response only.

Origin validation (scope 11) is verified against how the gateway ACTUALLY
matches an incoming Origin header: ``api.gateway.sessions.origin_allowed``
does an EXACT STRING match (``origin in allowed`` -- no scheme/host
parsing, no wildcard expansion, no path stripping). So the allowlist must
store full origin strings in the shape a browser's ``Origin`` header
actually takes: ``scheme://host[:port]``, no path, no wildcard, no
trailing slash -- otherwise a stored value could look plausible but never
actually match at admission time (a silent, confusing lockout).

An EMPTY allowlist is explicitly ALLOWED (D6) -- it disables the widget
for that tenant, a real and documented consequence, not a bug to guard
against here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from common.auth import AuthClaims, Role
from common.db import Database
from common.errors import AuthorizationError, ValidationError

from api.admin.repository import rotate_own_client_key

# Matches exactly what a browser's Origin header looks like: scheme + host
# (+ optional port), nothing else. No path, no query, no fragment, no
# wildcard, no trailing slash -- mirrors api.gateway.sessions.origin_allowed's
# exact-string-match contract (verified live, gateway/sessions.py).
_ORIGIN_PATTERN = re.compile(
    r"^https?://"
    r"[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?"
    r"(\.[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?)*"
    r"(:\d{1,5})?$"
)
_MAX_ORIGINS = 20


def _reject_non_client_admin(claims: AuthClaims) -> None:
    """API keys / Origin allowlist are CLIENT_ADMIN-only, own tenant (D6).

    Unlike most repositories here, this raises ``AuthorizationError``
    (403), not ``ValidationError`` -- this is an RBAC boundary (who may act),
    not a data-shape validation, and PLATFORM_ADMIN reaches this surface via
    a DIFFERENT existing route (the tenant-explicit rotate-key route, M7,
    untouched by this sprint) rather than through this CLIENT_ADMIN-only
    module.
    """
    if claims.role != Role.CLIENT_ADMIN or claims.tenant_id is None:
        raise AuthorizationError(
            "Only a CLIENT_ADMIN may manage their tenant's API keys.",
            code="ROLE_NOT_PERMITTED",
        )


def _validate_origin(origin: str) -> None:
    if not _ORIGIN_PATTERN.match(origin):
        raise ValidationError(
            f"{origin!r} is not a valid Origin (expected "
            "'scheme://host[:port]', no path, no wildcard, no trailing "
            "slash -- this must exact-match what a browser sends).",
            code="INVALID_ORIGIN",
        )


@dataclass(frozen=True)
class ApiKeyInfo:
    """CLIENT_ADMIN-facing view of the tenant's API key. NEVER carries key
    material -- the stored value is a one-way hash (M6) and the UI can only
    ever show a freshly-rotated raw key once, in the rotation response."""

    has_key: bool
    allowed_origins: list[str]


async def get_api_key_info(db: Database, claims: AuthClaims) -> ApiKeyInfo:
    """Fetch the caller's tenant's key existence + Origin allowlist.

    Never returns or derives the raw/hashed key -- only whether one exists.
    """
    _reject_non_client_admin(claims)

    row = await db.fetchrow(
        "SELECT client_key_hash, allowed_origins FROM tenants WHERE id = $1",
        claims.tenant_id,
    )
    if row is None:
        return ApiKeyInfo(has_key=False, allowed_origins=[])

    return ApiKeyInfo(
        has_key=bool(row["client_key_hash"]),
        allowed_origins=list(row["allowed_origins"] or []),
    )


async def rotate_own_key(db: Database, claims: AuthClaims) -> str:
    """Rotate the caller's OWN tenant's client key (SR-20 D6).

    Thin wrapper over ``api.admin.repository.rotate_own_client_key`` --
    reuses the exact same crypto/SQL as the shipped PLATFORM_ADMIN path
    (M6/M7), one mechanism, two RBAC-gated entry points. Returns the new
    RAW key -- callers MUST return it in the response body only, once, and
    never log it.
    """
    _reject_non_client_admin(claims)

    raw_key = await rotate_own_client_key(db, claims)
    if raw_key is None:  # pragma: no cover -- a CLIENT_ADMIN's own tenant always exists
        raise ValidationError("Workspace not found.", code="TENANT_NOT_FOUND")
    return raw_key


async def update_allowed_origins(
    db: Database, claims: AuthClaims, *, origins: list[str]
) -> ApiKeyInfo:
    """Replace the caller's tenant's Origin allowlist.

    Every origin is validated in Python BEFORE the DB (defense-in-depth,
    house pattern) against the EXACT shape the gateway's ``origin_allowed``
    exact-string match requires. An EMPTY list is explicitly allowed (D6) --
    it disables the widget for the tenant, a real, documented consequence,
    not something this function guards against.
    """
    _reject_non_client_admin(claims)

    if len(origins) > _MAX_ORIGINS:
        raise ValidationError(
            f"At most {_MAX_ORIGINS} origins are allowed.",
            code="TOO_MANY_ORIGINS",
        )
    for origin in origins:
        _validate_origin(origin)

    row = await db.fetchrow(
        "UPDATE tenants SET allowed_origins = $1, updated_at = now() "
        "WHERE id = $2 RETURNING allowed_origins",
        origins,
        claims.tenant_id,
    )
    if row is None:
        raise ValidationError("Workspace not found.", code="TENANT_NOT_FOUND")

    return ApiKeyInfo(has_key=True, allowed_origins=list(row["allowed_origins"] or []))

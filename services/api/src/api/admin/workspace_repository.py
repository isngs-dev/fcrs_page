"""Workspace settings repository (SR-20 D5) -- tenants.name / .slug / .timezone.

Mirrors ``api.admin.settings_repository``'s shape (never-``None`` resolver,
Python-validation-before-DB) exactly.

**D5, AS AMENDED**: the spec's original Data Model text also named a
``tenants.language`` column. The user's own "Open questions" section flagged
that a stored preference with no i18n consumer is uncomfortably close to the
silent-no-op D1 forbids, and the question has since been resolved: Language
renders disabled/coming-soon in the Settings UI (D7's pattern), exactly like
Billing and Delete-workspace -- NOT a live stored field. There is nothing to
store, so this module never reads or writes a ``language`` column, and the
migration (0045) never adds one.

Validation is defense-in-depth, in Python, BEFORE the DB (house pattern,
SR-9.4 M5/D10):
- ``timezone``: a real IANA ``Area/Location`` identifier (``Europe/London``)
  or the bare ``UTC`` zone -- never a raw/legacy fixed-offset alias
  (``GMT+0``, ``PST8PDT``, ``BST``). ``zoneinfo.available_timezones()``
  alone is NOT sufficient: it also contains those legacy POSIX-compatible
  aliases, which the spec explicitly requires rejecting.
- ``slug``: charset/length/reserved-value validated in Python first; a
  UNIQUE-constraint collision at the DB layer is still translated to a
  specific 422 (``WORKSPACE_SLUG_TAKEN``), never a raw 500, as a second line
  of defense against a race between the Python check and the INSERT/UPDATE.

Data model (migration 0045 -- read the live head first, D8):
- ``tenants.timezone`` (nullable, no backfill -- an unconfigured tenant
  resolves to ``settings.workspace_default_timezone`` at read time, never a
  guessed value written back as if configured).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from zoneinfo import ZoneInfoNotFoundError, available_timezones

import asyncpg
from common.auth import AuthClaims
from common.db import Database
from common.errors import ValidationError

from api.config import get_api_settings

_SLUG_PATTERN = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_RESERVED_SLUGS = frozenset(
    {"admin", "api", "www", "app", "widget", "static", "assets", "public"}
)

# Legacy/fixed-offset zoneinfo aliases that must NOT be accepted even though
# they resolve via zoneinfo -- e.g. "GMT+0", "PST8PDT". A real IANA
# geographic identifier always has an "Area/Location" shape; "UTC" is the
# one legitimate exception (unambiguous, DST-free, also our own platform
# default) and is allow-listed explicitly.
_UTC_ALIAS = "UTC"


def _is_valid_iana_timezone(value: str) -> bool:
    if value == _UTC_ALIAS:
        return True
    if "/" not in value:
        # Every real Area/Location IANA id contains a slash; bare names
        # ("GMT+0", "PST8PDT", "BST", "Israel", ...) are legacy aliases or
        # garbage, not what the spec means by "a real IANA identifier".
        return False
    return value in available_timezones()


@dataclass(frozen=True)
class Workspace:
    """A tenant's workspace settings (SR-20 D5). Never ``None`` for an
    unconfigured tenant -- ``timezone`` resolves to the platform default."""

    name: str
    slug: str
    timezone: str


def _reject_global(claims: AuthClaims) -> None:
    """Raise ``ValidationError`` for global callers (PLATFORM_ADMIN).

    Workspace settings are always tenant-scoped; a global caller has no
    tenant_id and therefore cannot be filtered to a tenant's row.
    """
    if claims.tenant_id is None:
        raise ValidationError(
            "Workspace settings are tenant-scoped; PLATFORM_ADMIN callers "
            "are not permitted.",
            code="GLOBAL_CALLER_NOT_PERMITTED",
        )


def _validate_timezone(timezone: str | None) -> None:
    if timezone is None:
        return
    if not _is_valid_iana_timezone(timezone):
        raise ValidationError(
            f"{timezone!r} is not a valid IANA timezone identifier "
            "(e.g. 'Europe/London'). Fixed offsets like 'GMT+0' are not "
            "accepted.",
            code="INVALID_TIMEZONE",
        )
    # Defense-in-depth: confirm ZoneInfo actually loads it (available_timezones()
    # is a name catalogue; this catches any environment-specific gaps).
    try:
        from zoneinfo import ZoneInfo  # noqa: PLC0415

        ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise ValidationError(
            f"{timezone!r} is not a valid IANA timezone identifier.",
            code="INVALID_TIMEZONE",
        ) from exc


def _validate_slug(slug: str) -> None:
    if not _SLUG_PATTERN.fullmatch(slug):
        raise ValidationError(
            "Workspace URL must be lowercase alphanumeric with single "
            "internal hyphens, 1-63 characters.",
            code="INVALID_SLUG",
        )
    if slug in _RESERVED_SLUGS:
        raise ValidationError(
            f"{slug!r} is a reserved workspace URL and cannot be used.",
            code="INVALID_SLUG",
        )


async def get_workspace(db: Database, claims: AuthClaims) -> Workspace:
    """Fetch the caller's tenant workspace settings.

    Never returns ``None``: an unset ``timezone`` resolves to
    ``settings.workspace_default_timezone`` (no backfill, no fabrication).
    ``name``/``slug`` always exist (NOT NULL columns since migration 0002).
    """
    _reject_global(claims)

    row = await db.fetchrow(
        "SELECT name, slug, timezone FROM tenants WHERE id = $1",
        claims.tenant_id,
    )
    if row is None:
        raise ValidationError(
            "Workspace not found for the current tenant.",
            code="TENANT_NOT_FOUND",
        )

    settings = get_api_settings()
    timezone = row["timezone"] or settings.workspace_default_timezone
    return Workspace(name=row["name"], slug=row["slug"], timezone=timezone)


async def update_workspace(
    db: Database,
    claims: AuthClaims,
    *,
    name: str,
    slug: str,
    timezone: str | None,
) -> Workspace:
    """Update the caller's tenant name / slug / timezone.

    Validates ``timezone`` and ``slug`` in Python BEFORE issuing any DB
    call (defense-in-depth). A ``slug`` UNIQUE collision at the DB layer is
    translated to ``ValidationError(code="WORKSPACE_SLUG_TAKEN")`` (422),
    never a raw 500 from an unhandled constraint violation.

    Never writes a ``language`` column/param (D5, amended -- see module
    docstring): Language is disabled/coming-soon in the UI, not a live
    stored field.
    """
    _reject_global(claims)
    _validate_slug(slug)
    _validate_timezone(timezone)

    try:
        row = await db.fetchrow(
            "UPDATE tenants SET name = $1, slug = $2, timezone = $3, "
            "updated_at = now() WHERE id = $4 "
            "RETURNING name, slug, timezone",
            name,
            slug,
            timezone,
            claims.tenant_id,
        )
    except asyncpg.UniqueViolationError as exc:
        raise ValidationError(
            "That workspace URL is already taken.",
            code="WORKSPACE_SLUG_TAKEN",
        ) from exc

    if row is None:
        raise ValidationError(
            "Workspace not found for the current tenant.",
            code="TENANT_NOT_FOUND",
        )

    settings = get_api_settings()
    resolved_timezone = row["timezone"] or settings.workspace_default_timezone
    return Workspace(name=row["name"], slug=row["slug"], timezone=resolved_timezone)

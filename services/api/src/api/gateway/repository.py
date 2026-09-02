"""Gateway repository -- pre-auth tenant resolution by client key.

``get_tenant_by_client_key`` is intentionally UNSCOPED (no ``tenant_id`` filter).
At widget-admission time there are no AuthClaims yet; the client key IS the tenant
selector, and it is public + Origin-guarded. This is the second legitimate
pre-auth database query (alongside ``auth.repository.get_user_by_email``).

Since S12.1 (migration 0030), ``tenants.client_key`` is stored as a SHA-256
hash (``client_key_hash``), not plaintext -- the incoming raw key is hashed
with ``api.admin.repository._hash_client_key`` (imported here, not
duplicated -- ``admin/`` is the module that *creates* keys, ``gateway/``
only *validates* them) before the lookup. Same signature, same return shape,
same ``None``-on-miss behavior; only the WHERE clause + the hash step
changed.

SR-3 decision 8/9/10: ``get_resume_enabled`` is the widget-session-continuity
opt-in flag's ONLY read path. It rides the EXISTING ``tenant_bot_settings
.business_hours`` JSONB column (S12.2, migration 0031) under the key
``"widget_session_resume"`` -- deliberately NOT a new column/migration (Open
question 1, locked default). A tenant with no ``tenant_bot_settings`` row, or
whose ``business_hours`` JSON lacks the key, or whose value is not literally
``true``, defaults to ``False`` -- opt-in, never a silent upgrade.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from common.db import Database

from api.admin.repository import _hash_client_key

Row = dict[str, Any]


@dataclass(frozen=True)
class WidgetBranding:
    """Optional per-tenant widget branding, read pre-auth at session mint."""

    bot_name: str | None
    accent_color: str | None
    launcher_position: str | None
    suggested_questions: list[str] | None


async def get_tenant_by_client_key(db: Database, client_key: str) -> Row | None:
    """Look up a tenant by its public client key (hashed lookup, S12.1).

    UNSCOPED by design -- this is the pre-auth tenant-resolution query.
    The client key is public; abuse protection comes from the Origin allowlist.
    Hashing here is corruption/leak hygiene, not a secrecy requirement.
    """
    sql = (
        "SELECT id, slug, enabled, allowed_origins "
        "FROM tenants WHERE client_key_hash = $1"
    )
    record = await db.fetchrow(sql, _hash_client_key(client_key))
    return dict(record) if record is not None else None


async def get_resume_enabled(db: Database, tenant_id: str) -> bool:
    """Read the ``widget_session_resume`` opt-in flag (SR-3 decision 8).

    Migration-free by design: reads the ``widget_session_resume`` boolean key
    out of the EXISTING ``tenant_bot_settings.business_hours`` JSONB blob
    (S12.2) rather than a new column. UNSCOPED by ``AuthClaims`` for the same
    pre-auth reason as ``get_tenant_by_client_key`` -- this runs during
    admission, before any claims exist; the caller already resolved
    ``tenant_id`` from the public client key + Origin allowlist. Returns
    ``False`` (opt-in default, decision 8) when there is no settings row, no
    ``business_hours`` JSON, or the key is absent/not ``True``.
    """
    row = await db.fetchrow(
        "SELECT business_hours FROM tenant_bot_settings WHERE tenant_id = $1",
        tenant_id,
    )
    if row is None:
        return False
    business_hours = row.get("business_hours") if hasattr(row, "get") else row["business_hours"]
    if not isinstance(business_hours, dict):
        return False
    return business_hours.get("widget_session_resume") is True


async def get_launcher_label(db: Database, tenant_id: str) -> str | None:
    """Read a resolved tenant's optional launcher label during admission.

    This is intentionally pre-auth and unscoped by ``AuthClaims``: the
    gateway has already selected the tenant from its public client key and
    Origin allowlist. The parameterized tenant predicate ensures the selected
    tenant can never receive another tenant's label.
    """
    row = await db.fetchrow(
        "SELECT launcher_label FROM tenant_bot_settings WHERE tenant_id = $1",
        tenant_id,
    )
    if row is None:
        return None
    value = row.get("launcher_label") if hasattr(row, "get") else row["launcher_label"]
    return value if isinstance(value, str) else None


async def get_widget_branding(db: Database, tenant_id: str) -> WidgetBranding:
    """Read a resolved tenant's optional widget branding during admission.

    Same pre-auth, unscoped-by-``AuthClaims`` reasoning as ``get_launcher_label``
    -- bundled into one query since all four fields are needed on every
    session mint.
    """
    row = await db.fetchrow(
        "SELECT bot_name, accent_color, launcher_position, suggested_questions "
        "FROM tenant_bot_settings WHERE tenant_id = $1",
        tenant_id,
    )
    if row is None:
        return WidgetBranding(
            bot_name=None, accent_color=None, launcher_position=None, suggested_questions=None
        )
    suggested_questions = row["suggested_questions"]
    return WidgetBranding(
        bot_name=row["bot_name"] if isinstance(row["bot_name"], str) else None,
        accent_color=row["accent_color"] if isinstance(row["accent_color"], str) else None,
        launcher_position=(
            row["launcher_position"] if isinstance(row["launcher_position"], str) else None
        ),
        suggested_questions=(
            suggested_questions if isinstance(suggested_questions, list) else None
        ),
    )

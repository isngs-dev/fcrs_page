"""Pydantic request/response models for the opportunities admin surface
(SR-9.4).

``amount`` is declared as ``Decimal | None`` everywhere -- never ``float``
(D7). Response models are leak-free: never carry ``tenant_id``, and never
carry the raw tenant config alongside a single opportunity (only the row's
own stamped ``currency`` and the derived ``win_probability``).
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel


class OpportunityCreateRequest(BaseModel):
    """Body for POST /admin/opportunities.

    ``contact_id`` is required (D6). ``account_id`` is optional -- when
    omitted, the route copies the contact's current ``account_id`` onto the
    row (a snapshot, not a live inheritance). ``currency`` is never accepted
    here -- it is always stamped server-side from the tenant's config (D7).
    ``stage`` is never accepted here -- creation always starts at
    ``'prospecting'`` (D1).
    """

    contact_id: str
    account_id: str | None = None
    name: str
    amount: Decimal | None = None
    expected_close_date: date | None = None
    owner_agent_id: str | None = None


class OpportunityUpdateRequest(BaseModel):
    """Body for PATCH /admin/opportunities/{id}. Only supplied fields change.

    Deliberately excludes ``stage`` (use ``POST .../stage``) and
    ``currency`` (immutable historical snapshot, D7).
    """

    name: str | None = None
    amount: Decimal | None = None
    expected_close_date: date | None = None
    owner_agent_id: str | None = None
    account_id: str | None = None


class OpportunityStageTransitionRequest(BaseModel):
    """Body for POST /admin/opportunities/{id}/stage.

    ``close_reason`` is required (non-empty, non-whitespace-only) when
    ``stage == "closed_lost"`` (D9); optional otherwise. Enforced by the
    route, not by this model, so the 422 carries the sprint's dedicated
    ``CLOSE_REASON_REQUIRED`` code rather than a generic Pydantic error.
    """

    stage: str
    close_reason: str | None = None


class OpportunityResponse(BaseModel):
    """Leak-free (no ``tenant_id``) opportunity for the admin surface.

    Carries the DERIVED ``win_probability`` (D2 -- never stored on the row)
    and the row's stamped ``currency`` (D7 -- a historical snapshot).
    """

    opportunity_id: str
    contact_id: str
    account_id: str | None
    name: str
    amount: Decimal | None
    currency: str
    stage: str
    win_probability: int
    expected_close_date: date | None
    closed_at: datetime | None
    close_reason: str | None
    owner_agent_id: str | None
    created_at: datetime


class OpportunityListResponse(BaseModel):
    """Paginated envelope for GET /admin/opportunities."""

    items: list[OpportunityResponse]
    total: int
    limit: int
    offset: int


class OpportunityConfigResponse(BaseModel):
    """Leak-free tenant opportunity config (currency + per-stage
    win-probabilities). Terminal stages are never included here -- they are
    fixed (100/closed_won, 0/closed_lost), not tenant-configurable (D3)."""

    currency: str
    stage_probabilities: dict[str, int]


class OpportunityConfigUpdateRequest(BaseModel):
    """Body for PUT /admin/opportunities/config (CLIENT_ADMIN only, D8)."""

    currency: str
    stage_probabilities: dict[str, int]

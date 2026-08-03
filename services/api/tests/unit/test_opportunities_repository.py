"""Unit tests for api.opportunities.repository (SR-9.4).

Covers:
- create_opportunity inserts a row starting at stage='prospecting'.
- get_opportunity / list_opportunities tenant isolation, every filter
  combination never widens scope.
- list_opportunities open_only excludes closed_won/closed_lost.
- update_opportunity: only supplied fields change (_UNSET sentinel);
  expected_close_date=None clears it; cannot change stage or currency
  (not accepted as kwargs at all).
- transition_stage: sets stage/close_reason/closed_at; returns None for
  missing/cross-tenant id.
- Amount round-trips as Decimal.
- _reject_global for every method.
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from common.auth import AuthClaims, Role
from common.errors import ValidationError

_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
_TENANT_A = "tenant-a"
_TENANT_B = "tenant-b"


class _StubDatabase:
    """In-memory stub database for testing the opportunities repository."""

    def __init__(self) -> None:
        self._opportunities: dict[tuple[str, str], dict[str, Any]] = {}

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        q = query.strip().upper()
        if "COUNT(*)" in q and "FROM OPPORTUNITIES" in q:
            return {"count": self._count_matching(query, args)}
        if "FROM OPPORTUNITIES" in q and "OPPORTUNITY_ID = $2" in q:
            tenant_id, opportunity_id = args[0], args[1]
            return self._opportunities.get((tenant_id, opportunity_id))
        return None

    def _count_matching(self, query: str, args: tuple[Any, ...]) -> int:
        rows = self._filtered_rows(query, args)
        return len(rows)

    def _filtered_rows(self, query: str, args: tuple[Any, ...]) -> list[dict[str, Any]]:
        tenant_id = args[0]
        rows = [r for r in self._opportunities.values() if r["tenant_id"] == tenant_id]
        idx = 1
        if "AND STAGE = $" in query.upper() and "NOT IN" not in query.upper().split("AND STAGE = $")[0]:
            pass
        q = query.upper()
        if " STAGE = $" in q:
            stage_val = args[idx]
            rows = [r for r in rows if r["stage"] == stage_val]
            idx += 1
        if " CONTACT_ID = $" in q:
            contact_val = args[idx]
            rows = [r for r in rows if r["contact_id"] == contact_val]
            idx += 1
        if " ACCOUNT_ID = $" in q:
            account_val = args[idx]
            rows = [r for r in rows if r["account_id"] == account_val]
            idx += 1
        if " OWNER_AGENT_ID = $" in q:
            owner_val = args[idx]
            rows = [r for r in rows if r["owner_agent_id"] == owner_val]
            idx += 1
        if "STAGE NOT IN ('CLOSED_WON','CLOSED_LOST')" in q:
            rows = [r for r in rows if r["stage"] not in ("closed_won", "closed_lost")]
        return rows

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        q = query.strip().upper()
        if "FROM OPPORTUNITIES" in q:
            # last two positional args are limit/offset when LIMIT present
            if " LIMIT $" in q:
                limit = args[-2]
                offset = args[-1]
                base_args = args[:-2]
            else:
                limit = None
                offset = 0
                base_args = args
            rows = self._filtered_rows(query, base_args)
            rows.sort(key=lambda r: (r["created_at"], r["opportunity_id"]), reverse=True)
            if limit is not None:
                rows = rows[offset : offset + limit]
            return rows
        return []

    async def execute(self, query: str, *args: Any) -> str:
        q = query.strip().upper()
        if q.startswith("INSERT INTO OPPORTUNITIES"):
            (
                tenant_id, opportunity_id, contact_id, account_id, name,
                amount, currency, expected_close_date, owner_agent_id,
            ) = args
            self._opportunities[(tenant_id, opportunity_id)] = {
                "tenant_id": tenant_id,
                "opportunity_id": opportunity_id,
                "contact_id": contact_id,
                "account_id": account_id,
                "name": name,
                "amount": amount,
                "currency": currency,
                "stage": "prospecting",
                "expected_close_date": expected_close_date,
                "closed_at": None,
                "close_reason": None,
                "owner_agent_id": owner_agent_id,
                "created_at": _NOW,
                "updated_at": _NOW,
            }
            return "INSERT 0 1"
        if q.startswith("UPDATE OPPORTUNITIES SET STAGE = $1"):
            stage, close_reason, closed_at, tenant_id, opportunity_id = args
            existing = self._opportunities.get((tenant_id, opportunity_id))
            if existing is None:
                return "UPDATE 0"
            existing["stage"] = stage
            existing["close_reason"] = close_reason
            existing["closed_at"] = closed_at
            existing["updated_at"] = _NOW
            return "UPDATE 1"
        if q.startswith("UPDATE OPPORTUNITIES"):
            tenant_id = args[-2]
            opportunity_id = args[-1]
            existing = self._opportunities.get((tenant_id, opportunity_id))
            if existing is None:
                return "UPDATE 0"
            set_part = query.split("SET", 1)[1].split("WHERE", 1)[0]
            columns = [c.strip().split("=")[0].strip() for c in set_part.split(",")]
            for col, val in zip(columns, args[:-2], strict=False):
                if col == "updated_at":
                    continue
                existing[col] = val
            existing["updated_at"] = _NOW
            return "UPDATE 1"
        return "OK"

    def seed(
        self,
        *,
        tenant_id: str,
        opportunity_id: str,
        contact_id: str = "contact-1",
        account_id: str | None = None,
        name: str = "Test deal",
        amount: Decimal | None = Decimal("100.00"),
        currency: str = "USD",
        stage: str = "prospecting",
        owner_agent_id: str | None = None,
        expected_close_date: date | None = None,
    ) -> None:
        self._opportunities[(tenant_id, opportunity_id)] = {
            "tenant_id": tenant_id,
            "opportunity_id": opportunity_id,
            "contact_id": contact_id,
            "account_id": account_id,
            "name": name,
            "amount": amount,
            "currency": currency,
            "stage": stage,
            "expected_close_date": expected_close_date,
            "closed_at": None,
            "close_reason": None,
            "owner_agent_id": owner_agent_id,
            "created_at": _NOW,
            "updated_at": _NOW,
        }


def _claims(tenant_id: str | None = _TENANT_A, role: Role = Role.CLIENT_ADMIN) -> AuthClaims:
    return AuthClaims(subject="user-1", role=role, tenant_id=tenant_id)


# ---------------------------------------------------------------------------
# create_opportunity / get_opportunity
# ---------------------------------------------------------------------------


async def test_create_opportunity_starts_at_prospecting() -> None:
    from api.opportunities.repository import create_opportunity, get_opportunity

    db = _StubDatabase()
    opp_id = await create_opportunity(
        db, _claims(), contact_id="contact-1", account_id=None,
        name="Roof job", amount=Decimal("12500.00"), currency="USD",
    )

    fetched = await get_opportunity(db, _claims(), opp_id)
    assert fetched is not None
    assert fetched.stage == "prospecting"
    assert fetched.amount == Decimal("12500.00")
    assert fetched.currency == "USD"
    assert fetched.account_id is None


async def test_amount_round_trips_as_exact_decimal() -> None:
    from api.opportunities.repository import create_opportunity, get_opportunity

    db = _StubDatabase()
    opp_id = await create_opportunity(
        db, _claims(), contact_id="contact-1", account_id=None,
        name="Deal", amount=Decimal("19.99"), currency="USD",
    )
    fetched = await get_opportunity(db, _claims(), opp_id)
    assert fetched is not None
    assert fetched.amount == Decimal("19.99")
    assert isinstance(fetched.amount, Decimal)


async def test_create_opportunity_null_amount_accepted() -> None:
    from api.opportunities.repository import create_opportunity, get_opportunity

    db = _StubDatabase()
    opp_id = await create_opportunity(
        db, _claims(), contact_id="contact-1", account_id=None,
        name="Unquoted deal", amount=None, currency="USD",
    )
    fetched = await get_opportunity(db, _claims(), opp_id)
    assert fetched is not None
    assert fetched.amount is None


async def test_get_cross_tenant_returns_none() -> None:
    from api.opportunities.repository import get_opportunity

    db = _StubDatabase()
    db.seed(tenant_id=_TENANT_A, opportunity_id="opp-1")

    result = await get_opportunity(db, _claims(_TENANT_B), "opp-1")
    assert result is None


async def test_get_rejects_global_caller() -> None:
    from api.opportunities.repository import get_opportunity

    db = _StubDatabase()
    with pytest.raises(ValidationError) as exc_info:
        await get_opportunity(db, _claims(None, Role.PLATFORM_ADMIN), "opp-1")
    assert exc_info.value.code == "GLOBAL_CALLER_NOT_PERMITTED"


# ---------------------------------------------------------------------------
# list_opportunities -- tenant isolation + filters
# ---------------------------------------------------------------------------


async def test_list_never_returns_other_tenants_rows() -> None:
    from api.opportunities.repository import list_opportunities

    db = _StubDatabase()
    db.seed(tenant_id=_TENANT_A, opportunity_id="opp-a1")
    db.seed(tenant_id=_TENANT_B, opportunity_id="opp-b1")

    rows, total = await list_opportunities(db, _claims(_TENANT_A))
    assert total == 1
    assert [r.opportunity_id for r in rows] == ["opp-a1"]


@pytest.mark.parametrize(
    "filter_kwargs",
    [
        {"stage": "prospecting"},
        {"contact_id": "contact-1"},
        {"account_id": "acct-1"},
        {"owner_agent_id": "agent-1"},
        {"open_only": True},
    ],
)
async def test_every_filter_never_widens_scope_across_tenants(filter_kwargs: dict[str, Any]) -> None:
    from api.opportunities.repository import list_opportunities

    db = _StubDatabase()
    db.seed(
        tenant_id=_TENANT_A, opportunity_id="opp-a1", contact_id="contact-1",
        account_id="acct-1", owner_agent_id="agent-1", stage="prospecting",
    )
    db.seed(
        tenant_id=_TENANT_B, opportunity_id="opp-b1", contact_id="contact-1",
        account_id="acct-1", owner_agent_id="agent-1", stage="prospecting",
    )

    rows, total = await list_opportunities(db, _claims(_TENANT_A), **filter_kwargs)
    assert all(r.opportunity_id != "opp-b1" for r in rows)
    assert total <= 1


async def test_open_only_excludes_closed_deals() -> None:
    from api.opportunities.repository import list_opportunities

    db = _StubDatabase()
    db.seed(tenant_id=_TENANT_A, opportunity_id="opp-open", stage="proposal")
    db.seed(tenant_id=_TENANT_A, opportunity_id="opp-won", stage="closed_won")
    db.seed(tenant_id=_TENANT_A, opportunity_id="opp-lost", stage="closed_lost")

    rows, total = await list_opportunities(db, _claims(_TENANT_A), open_only=True)
    assert total == 1
    assert rows[0].opportunity_id == "opp-open"


async def test_open_only_false_includes_closed_by_default() -> None:
    from api.opportunities.repository import list_opportunities

    db = _StubDatabase()
    db.seed(tenant_id=_TENANT_A, opportunity_id="opp-open", stage="proposal")
    db.seed(tenant_id=_TENANT_A, opportunity_id="opp-won", stage="closed_won")

    rows, total = await list_opportunities(db, _claims(_TENANT_A))
    assert total == 2


async def test_contact_can_hold_several_simultaneous_open_opportunities() -> None:
    from api.opportunities.repository import list_opportunities

    db = _StubDatabase()
    db.seed(tenant_id=_TENANT_A, opportunity_id="opp-1", contact_id="contact-1")
    db.seed(tenant_id=_TENANT_A, opportunity_id="opp-2", contact_id="contact-1")
    db.seed(tenant_id=_TENANT_A, opportunity_id="opp-3", contact_id="contact-1")

    rows, total = await list_opportunities(db, _claims(_TENANT_A), contact_id="contact-1")
    assert total == 3


async def test_list_rejects_global_caller() -> None:
    from api.opportunities.repository import list_opportunities

    db = _StubDatabase()
    with pytest.raises(ValidationError) as exc_info:
        await list_opportunities(db, _claims(None, Role.PLATFORM_ADMIN))
    assert exc_info.value.code == "GLOBAL_CALLER_NOT_PERMITTED"


# ---------------------------------------------------------------------------
# update_opportunity
# ---------------------------------------------------------------------------


async def test_update_only_supplied_fields_change() -> None:
    from api.opportunities.repository import update_opportunity

    db = _StubDatabase()
    db.seed(tenant_id=_TENANT_A, opportunity_id="opp-1", name="Original", amount=Decimal("100.00"))

    updated = await update_opportunity(db, _claims(_TENANT_A), "opp-1", name="Updated")
    assert updated is not None
    assert updated.name == "Updated"
    assert updated.amount == Decimal("100.00")


async def test_update_expected_close_date_null_clears_it() -> None:
    from api.opportunities.repository import update_opportunity

    db = _StubDatabase()
    db.seed(tenant_id=_TENANT_A, opportunity_id="opp-1", expected_close_date=date(2026, 9, 30))

    updated = await update_opportunity(db, _claims(_TENANT_A), "opp-1", expected_close_date=None)
    assert updated is not None
    assert updated.expected_close_date is None


async def test_update_omitted_field_left_untouched() -> None:
    from api.opportunities.repository import update_opportunity

    db = _StubDatabase()
    db.seed(tenant_id=_TENANT_A, opportunity_id="opp-1", expected_close_date=date(2026, 9, 30))

    updated = await update_opportunity(db, _claims(_TENANT_A), "opp-1", name="New name")
    assert updated is not None
    assert updated.expected_close_date == date(2026, 9, 30)


async def test_update_cannot_change_stage_or_currency_not_accepted_as_kwargs() -> None:
    """update_opportunity's signature has no stage or currency parameter at
    all -- this is a structural guarantee, not a runtime check."""
    import inspect

    from api.opportunities.repository import update_opportunity

    sig = inspect.signature(update_opportunity)
    assert "stage" not in sig.parameters
    assert "currency" not in sig.parameters


async def test_update_cross_tenant_returns_none_and_does_not_mutate() -> None:
    from api.opportunities.repository import update_opportunity

    db = _StubDatabase()
    db.seed(tenant_id=_TENANT_A, opportunity_id="opp-1", name="Original")

    result = await update_opportunity(db, _claims(_TENANT_B), "opp-1", name="Hacked")
    assert result is None
    assert db._opportunities[(_TENANT_A, "opp-1")]["name"] == "Original"


async def test_update_rejects_global_caller() -> None:
    from api.opportunities.repository import update_opportunity

    db = _StubDatabase()
    with pytest.raises(ValidationError) as exc_info:
        await update_opportunity(db, _claims(None, Role.PLATFORM_ADMIN), "opp-1", name="X")
    assert exc_info.value.code == "GLOBAL_CALLER_NOT_PERMITTED"


# ---------------------------------------------------------------------------
# transition_stage
# ---------------------------------------------------------------------------


async def test_transition_stage_sets_stage_and_closed_at() -> None:
    from api.opportunities.repository import transition_stage

    db = _StubDatabase()
    db.seed(tenant_id=_TENANT_A, opportunity_id="opp-1", stage="negotiation")

    updated = await transition_stage(
        db, _claims(_TENANT_A), "opp-1",
        stage="closed_lost", close_reason="Lost to competitor", closed_at=_NOW,
    )
    assert updated is not None
    assert updated.stage == "closed_lost"
    assert updated.close_reason == "Lost to competitor"
    assert updated.closed_at == _NOW


async def test_transition_stage_non_terminal_leaves_closed_at_null() -> None:
    from api.opportunities.repository import transition_stage

    db = _StubDatabase()
    db.seed(tenant_id=_TENANT_A, opportunity_id="opp-1", stage="prospecting")

    updated = await transition_stage(
        db, _claims(_TENANT_A), "opp-1",
        stage="qualification", close_reason=None, closed_at=None,
    )
    assert updated is not None
    assert updated.closed_at is None


async def test_transition_stage_cross_tenant_returns_none() -> None:
    from api.opportunities.repository import transition_stage

    db = _StubDatabase()
    db.seed(tenant_id=_TENANT_A, opportunity_id="opp-1", stage="prospecting")

    result = await transition_stage(
        db, _claims(_TENANT_B), "opp-1",
        stage="qualification", close_reason=None, closed_at=None,
    )
    assert result is None
    assert db._opportunities[(_TENANT_A, "opp-1")]["stage"] == "prospecting"


async def test_transition_stage_rejects_global_caller() -> None:
    from api.opportunities.repository import transition_stage

    db = _StubDatabase()
    with pytest.raises(ValidationError) as exc_info:
        await transition_stage(
            db, _claims(None, Role.PLATFORM_ADMIN), "opp-1",
            stage="qualification", close_reason=None, closed_at=None,
        )
    assert exc_info.value.code == "GLOBAL_CALLER_NOT_PERMITTED"

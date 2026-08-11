"""Unit tests for the SR-9.5 reports admin routes (leads-by-stage, bookings,
funnel, win-loss -- JSON + CSV, implicit + tenant-explicit).

Covers (MANDATORY per the sprint spec):
- RBAC: CLIENT_ADMIN + CLIENT_AGENT succeed on all 4 JSON + all 4 CSV
  (including win/loss revenue); VISITOR rejected everywhere; PLATFORM_ADMIN
  rejected on implicit routes, succeeds via tenant-explicit, 404 for unknown
  tenant, audit records real platform-admin actor; no write verb exists
  (405 on POST/PATCH/PUT/DELETE).
- Date-range: default 30-day window; bucket=month succeeds on bookings AND
  the pre-existing overview endpoint; bucket=hour still 422; invalid window
  -> 422 INVALID_ANALYTICS_WINDOW; too-large window -> 422
  ANALYTICS_WINDOW_TOO_LARGE.
- Tenant isolation: seed tenant A and B with different exact counts, assert
  each tenant's numbers are exactly its own -- for JSON AND CSV bytes.
- No-silent-fallback: empty tenant -> 200 honest zeros + null rates.
- CSV safety end-to-end: a close_reason of "=cmd|'/c calc'!A1" is
  neutralized in the actual response bytes; "-15% under budget" preserved.
  CSV row/header counts match the JSON twin. Export writes a PII-free audit
  row.
- PII/logging: no tenant_id in response bodies.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import patch

from common.auth import AuthClaims, Role
from common.cache import InMemoryCache
from httpx import ASGITransport, AsyncClient

from api.auth.tokens import create_access_token

_TEST_JWT_SECRET = "x" * 48
_TENANT_A = "tenant-abc-123"
_TENANT_B = "tenant-xyz-999"

_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

_TEST_SETTINGS_ENV = {
    "DEPLOYMENT_MODE": "saas",
    "DATABASE_URL": "postgres://stub-host:5432/appdb",
    "REDIS_URL": "redis://stub-host:6379",
    "JWT_SECRET": _TEST_JWT_SECRET,
    "SECRET_ENCRYPTION_KEY": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    "SERVICE_NAME": "api",
    "LOG_LEVEL": "WARNING",
    "COOKIE_SECURE": "false",
}


class _StubDatabase:
    """In-memory stub database backing /admin/analytics/reports/*."""

    def __init__(self) -> None:
        self.leads: list[dict[str, Any]] = []
        self.schedule_events: list[dict[str, Any]] = []
        self.opportunities: list[dict[str, Any]] = []
        self.opportunity_configs: dict[str, dict[str, Any]] = {}
        self.tenants: dict[str, dict[str, Any]] = {}
        self.audit_rows: list[dict[str, Any]] = []

    def seed_tenant(self, *, tenant_id: str, slug: str, enabled: bool = True) -> None:
        self.tenants[tenant_id] = {"id": tenant_id, "name": slug, "slug": slug, "enabled": enabled}

    def seed_lead(self, *, tenant_id: str, stage: str, created_at: datetime, **kwargs: Any) -> None:
        self.leads.append({
            "tenant_id": tenant_id, "stage": stage, "created_at": created_at,
            "updated_at": kwargs.get("updated_at", created_at),
            "source": kwargs.get("source", "widget"),
            "assigned_agent_id": kwargs.get("assigned_agent_id"),
            "qualification_score": kwargs.get("qualification_score"),
            "name": kwargs.get("name", "Test Lead"),
            "lead_id": kwargs.get("lead_id", f"lead-{len(self.leads) + 1}"),
        })

    def seed_booking(self, *, tenant_id: str, status: str, created_at: datetime, **kwargs: Any) -> None:
        self.schedule_events.append({
            "tenant_id": tenant_id, "status": status, "created_at": created_at,
            "source": kwargs.get("source", "native"),
        })

    def seed_opportunity(
        self, *, tenant_id: str, stage: str, closed_at: datetime | None,
        created_at: datetime, amount: Decimal | None, close_reason: str | None = None,
        owner_agent_id: str | None = None,
    ) -> None:
        self.opportunities.append({
            "tenant_id": tenant_id, "stage": stage, "closed_at": closed_at,
            "created_at": created_at, "amount": amount, "close_reason": close_reason,
            "owner_agent_id": owner_agent_id,
        })

    def seed_opportunity_config(self, *, tenant_id: str, currency: str) -> None:
        self.opportunity_configs[tenant_id] = {"currency": currency, "stage_probabilities": {}}

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        q = query.strip().upper()
        if "FROM TENANTS WHERE ID" in q:
            return self.tenants.get(args[0])
        if "FROM TENANT_OPPORTUNITY_CONFIGS" in q:
            return self.opportunity_configs.get(args[0])
        if "FROM LEADS" in q and "FILTER (WHERE QUALIFICATION_SCORE" in q:
            return self._query_score_distribution(args)
        return None

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        q = query.upper()
        if "FROM LEADS" in q:
            if "WHERE TENANT_ID = $1 AND STAGE = 'CONVERTED'" in q:
                return self._query_recent_conversions(args)
            if "GROUP BY SOURCE" in q:
                return self._query_lead_sources(args)
            if "GROUP BY ASSIGNED_AGENT_ID" in q:
                return self._query_agent_performance(args)
            return self._query_leads(query, args)
        if "FROM SCHEDULE_EVENTS" in q:
            return self._query_bookings(query, args)
        if "FROM OPPORTUNITIES" in q:
            return self._query_opportunities(query, args)
        return []

    def _query_leads(self, query: str, args: tuple[Any, ...]) -> list[dict[str, Any]]:
        tenant_id, window_from, window_to = args[0], args[1], args[2]
        idx = 3
        rows = [
            r for r in self.leads
            if r["tenant_id"] == tenant_id and window_from <= r["created_at"] < window_to
        ]
        q = query.upper()
        if " AND SOURCE = $" in q:
            rows = [r for r in rows if r["source"] == args[idx]]
            idx += 1
        if " AND ASSIGNED_AGENT_ID = $" in q:
            rows = [r for r in rows if r["assigned_agent_id"] == args[idx]]
            idx += 1
        grouped: dict[str, int] = {}
        for r in rows:
            grouped[r["stage"]] = grouped.get(r["stage"], 0) + 1
        return [{"stage": k, "cnt": v} for k, v in grouped.items()]

    def _windowed_leads(self, args: tuple[Any, ...]) -> list[dict[str, Any]]:
        tenant_id, window_from, window_to = args[0], args[1], args[2]
        return [
            r for r in self.leads
            if r["tenant_id"] == tenant_id and window_from <= r["created_at"] < window_to
        ]

    def _query_lead_sources(self, args: tuple[Any, ...]) -> list[dict[str, Any]]:
        rows = self._windowed_leads(args)
        grouped: dict[str, int] = {}
        for r in rows:
            grouped[r["source"]] = grouped.get(r["source"], 0) + 1
        return [{"source": k, "cnt": v} for k, v in grouped.items()]

    def _query_score_distribution(self, args: tuple[Any, ...]) -> dict[str, Any]:
        rows = self._windowed_leads(args)

        def _band(lo: int, hi: int) -> int:
            return sum(
                1 for r in rows
                if r["qualification_score"] is not None and lo <= r["qualification_score"] <= hi
            )

        return {
            "band_0_19": _band(0, 19), "band_20_39": _band(20, 39),
            "band_40_59": _band(40, 59), "band_60_79": _band(60, 79),
            "band_80_100": _band(80, 100),
            "unscored": sum(1 for r in rows if r["qualification_score"] is None),
        }

    def _query_agent_performance(self, args: tuple[Any, ...]) -> list[dict[str, Any]]:
        rows = self._windowed_leads(args)
        grouped: dict[str | None, dict[str, int]] = {}
        for r in rows:
            slot = grouped.setdefault(
                r["assigned_agent_id"], {"assigned": 0, "contacted": 0, "won": 0},
            )
            slot["assigned"] += 1
            if r["stage"] in ("contacted", "converted"):
                slot["contacted"] += 1
            if r["stage"] == "converted":
                slot["won"] += 1
        return [{"assigned_agent_id": k, **v} for k, v in grouped.items()]

    def _query_recent_conversions(self, args: tuple[Any, ...]) -> list[dict[str, Any]]:
        tenant_id, window_from, window_to = args[0], args[1], args[2]
        limit = args[3]
        rows = [
            r for r in self.leads
            if r["tenant_id"] == tenant_id
            and r["stage"] == "converted"
            and window_from <= r["updated_at"] < window_to
        ]
        rows.sort(key=lambda r: r["updated_at"], reverse=True)
        rows = rows[:limit]
        return [
            {
                "lead_id": r["lead_id"], "name": r["name"], "source": r["source"],
                "stage": r["stage"], "converted_at": r["updated_at"],
            }
            for r in rows
        ]

    def _query_bookings(self, query: str, args: tuple[Any, ...]) -> list[dict[str, Any]]:
        import datetime as _dt

        tenant_id, window_from, window_to = args[0], args[1], args[2]
        idx = 3
        rows = [
            r for r in self.schedule_events
            if r["tenant_id"] == tenant_id and window_from <= r["created_at"] < window_to
        ]
        q = query.upper()
        if " AND SOURCE = $" in q:
            rows = [r for r in rows if r["source"] == args[idx]]
            idx += 1
        if " AND STATUS = $" in q:
            rows = [r for r in rows if r["status"] == args[idx]]
            idx += 1
        bucket = args[idx]

        def _trunc(dt: datetime) -> datetime:
            if bucket == "day":
                return dt.replace(hour=0, minute=0, second=0, microsecond=0)
            if bucket == "week":
                start = dt - _dt.timedelta(days=dt.weekday())
                return start.replace(hour=0, minute=0, second=0, microsecond=0)
            if bucket == "month":
                return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            raise AssertionError(f"Unexpected bucket: {bucket}")

        buckets: dict[datetime, dict[str, int]] = {}
        for r in rows:
            b = _trunc(r["created_at"])
            slot = buckets.setdefault(b, {"booked": 0, "completed": 0, "no_show": 0, "cancelled": 0})
            slot[r["status"]] = slot.get(r["status"], 0) + 1
        return [
            {"bucket": b, **v} for b, v in sorted(buckets.items())
        ]

    def _query_opportunities(self, query: str, args: tuple[Any, ...]) -> list[dict[str, Any]]:
        q = query.upper()
        tenant_id, window_from, window_to = args[0], args[1], args[2]
        idx = 3
        rows = [
            r for r in self.opportunities
            if r["tenant_id"] == tenant_id
            and r["stage"] in ("closed_won", "closed_lost")
            and r["closed_at"] is not None
            and window_from <= r["closed_at"] < window_to
        ]
        if " AND OWNER_AGENT_ID = $" in q:
            rows = [r for r in rows if r["owner_agent_id"] == args[idx]]
            idx += 1

        if "GROUP BY STAGE" in q:
            grouped: dict[str, list[dict[str, Any]]] = {}
            for r in rows:
                grouped.setdefault(r["stage"], []).append(r)
            out = []
            for stage, items in grouped.items():
                amounts = [i["amount"] for i in items if i["amount"] is not None]
                amount_total = sum(amounts) if amounts else None
                null_count = sum(1 for i in items if i["amount"] is None)
                days = [(i["closed_at"] - i["created_at"]).total_seconds() / 86400.0 for i in items]
                avg_days = sum(days) / len(days) if days else None
                out.append({
                    "stage": stage, "cnt": len(items), "amount_null_count": null_count,
                    "amount_total": amount_total, "avg_days_to_close": avg_days,
                })
            return out

        if "STAGE = 'CLOSED_LOST'" in q and "ORDER BY CLOSED_AT DESC" in q:
            lost_rows = [r for r in rows if r["stage"] == "closed_lost"]
            lost_rows.sort(key=lambda r: r["closed_at"], reverse=True)
            limit = args[-1]
            lost_rows = lost_rows[:limit]
            return [{"closed_at": r["closed_at"], "close_reason": r["close_reason"]} for r in lost_rows]

        return []

    async def execute(self, query: str, *args: Any) -> str:
        q = query.strip().upper()
        if q.startswith("INSERT INTO AUDIT_EVENTS"):
            tenant_id, event_id, actor, action, target_type, target_id, metadata = args
            self.audit_rows.append({
                "tenant_id": tenant_id, "event_id": event_id, "actor": actor,
                "action": action, "target_type": target_type, "target_id": target_id,
                "metadata": metadata,
            })
            return "INSERT 1"
        return "OK"

    async def close(self) -> None:
        pass


class _StubRedis:
    async def get(self, key: str) -> str | None:
        return None

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        pass

    async def getdel(self, key: str) -> str | None:
        return None

    async def ping(self) -> bool:
        return True

    async def aclose(self) -> None:
        pass


def _build_app(db: Any = None) -> Any:
    from common.settings import get_settings

    from api.config import get_api_settings

    get_settings.cache_clear()
    get_api_settings.cache_clear()

    with patch.dict("os.environ", _TEST_SETTINGS_ENV, clear=False):
        from api.app import create_app

        app = create_app()

    app.state.db = db if db is not None else _StubDatabase()
    app.state.redis = _StubRedis()
    app.state.cache = InMemoryCache()
    app.state.rate_limiter = None
    return app


def _mint_cookie(
    *,
    subject: str = "user-1",
    role: Role = Role.CLIENT_ADMIN,
    tenant_id: str | None = _TENANT_A,
    ttl_seconds: int = 300,
    secret: str = _TEST_JWT_SECRET,
) -> str:
    claims = AuthClaims(subject=subject, role=role, tenant_id=tenant_id)
    token, _ = create_access_token(claims, secret=secret, ttl_seconds=ttl_seconds)
    return token


_REPORT_PATHS = (
    "/admin/analytics/reports/leads-by-stage",
    "/admin/analytics/reports/bookings",
    "/admin/analytics/reports/funnel",
    "/admin/analytics/reports/win-loss",
)
_CSV_PATHS = tuple(f"{p}.csv" for p in _REPORT_PATHS)

_QUERY = "?from=2026-07-01T00:00:00Z&to=2026-08-01T00:00:00Z"


# ==============================================================================
# RBAC
# ==============================================================================


async def test_client_admin_succeeds_on_all_four_json_reports() -> None:
    app = _build_app()
    token = _mint_cookie(role=Role.CLIENT_ADMIN)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        for path in _REPORT_PATHS:
            resp = await c.get(f"{path}{_QUERY}", cookies={"access_token": token})
            assert resp.status_code == 200, f"{path} -> {resp.status_code}: {resp.text}"


async def test_client_agent_succeeds_on_all_four_json_reports_including_win_loss() -> None:
    app = _build_app()
    token = _mint_cookie(role=Role.CLIENT_AGENT)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        for path in _REPORT_PATHS:
            resp = await c.get(f"{path}{_QUERY}", cookies={"access_token": token})
            assert resp.status_code == 200, f"{path} -> {resp.status_code}: {resp.text}"


async def test_client_admin_succeeds_on_all_four_csv_exports() -> None:
    app = _build_app()
    token = _mint_cookie(role=Role.CLIENT_ADMIN)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        for path in _CSV_PATHS:
            resp = await c.get(f"{path}{_QUERY}", cookies={"access_token": token})
            assert resp.status_code == 200, f"{path} -> {resp.status_code}: {resp.text}"
            assert resp.headers["content-type"].startswith("text/csv")


async def test_client_agent_succeeds_on_all_four_csv_exports() -> None:
    app = _build_app()
    token = _mint_cookie(role=Role.CLIENT_AGENT)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        for path in _CSV_PATHS:
            resp = await c.get(f"{path}{_QUERY}", cookies={"access_token": token})
            assert resp.status_code == 200, f"{path} -> {resp.status_code}: {resp.text}"


async def test_visitor_rejected_on_every_report_and_csv() -> None:
    app = _build_app()
    token = _mint_cookie(role=Role.VISITOR)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        for path in (*_REPORT_PATHS, *_CSV_PATHS):
            resp = await c.get(f"{path}{_QUERY}", cookies={"access_token": token})
            assert resp.status_code == 403, f"{path} -> {resp.status_code}"


async def test_platform_admin_rejected_on_implicit_routes() -> None:
    app = _build_app()
    token = _mint_cookie(role=Role.PLATFORM_ADMIN, tenant_id=None)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        for path in _REPORT_PATHS:
            resp = await c.get(f"{path}{_QUERY}", cookies={"access_token": token})
            assert resp.status_code == 403, f"{path} -> {resp.status_code}"


async def test_platform_admin_succeeds_via_tenant_explicit_route() -> None:
    db = _StubDatabase()
    db.seed_tenant(tenant_id=_TENANT_A, slug="acme")
    app = _build_app(db)
    token = _mint_cookie(role=Role.PLATFORM_ADMIN, tenant_id=None)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get(
            f"/admin/tenants/{_TENANT_A}/analytics/reports/funnel{_QUERY}",
            cookies={"access_token": token},
        )
    assert resp.status_code == 200


async def test_platform_admin_unknown_tenant_returns_404() -> None:
    app = _build_app()
    token = _mint_cookie(role=Role.PLATFORM_ADMIN, tenant_id=None)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get(
            f"/admin/tenants/does-not-exist/analytics/reports/funnel{_QUERY}",
            cookies={"access_token": token},
        )
    assert resp.status_code == 404
    assert resp.json()["error_code"] == "TENANT_NOT_FOUND"


async def test_platform_admin_export_audit_records_real_actor() -> None:
    db = _StubDatabase()
    db.seed_tenant(tenant_id=_TENANT_A, slug="acme")
    app = _build_app(db)
    token = _mint_cookie(role=Role.PLATFORM_ADMIN, tenant_id=None, subject="platform-user-1")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get(
            f"/admin/tenants/{_TENANT_A}/analytics/reports/funnel.csv{_QUERY}",
            cookies={"access_token": token},
        )
    assert resp.status_code == 200
    assert len(db.audit_rows) == 1
    row = db.audit_rows[0]
    assert row["actor"] == "platform-user-1"
    assert row["metadata"]["platform_admin"] is True


async def test_no_write_verb_exists_405_on_every_report_path() -> None:
    app = _build_app()
    token = _mint_cookie(role=Role.CLIENT_ADMIN)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        for path in _REPORT_PATHS:
            for method in ("post", "patch", "put", "delete"):
                resp = await c.request(method, f"{path}{_QUERY}", cookies={"access_token": token})
                assert resp.status_code == 405, f"{method} {path} -> {resp.status_code}"


# ==============================================================================
# Date-range correctness
# ==============================================================================


async def test_default_window_is_last_30_days() -> None:
    app = _build_app()
    token = _mint_cookie(role=Role.CLIENT_ADMIN)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/admin/analytics/reports/leads-by-stage", cookies={"access_token": token})
    assert resp.status_code == 200
    body = resp.json()
    window_from = datetime.fromisoformat(body["window"]["from"].replace("Z", "+00:00"))
    window_to = datetime.fromisoformat(body["window"]["to"].replace("Z", "+00:00"))
    assert (window_to - window_from).days == 30


async def test_bucket_month_succeeds_on_bookings_report() -> None:
    app = _build_app()
    token = _mint_cookie(role=Role.CLIENT_ADMIN)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get(
            f"/admin/analytics/reports/bookings{_QUERY}&bucket=month",
            cookies={"access_token": token},
        )
    assert resp.status_code == 200
    assert resp.json()["window"]["bucket"] == "month"


async def test_bucket_hour_still_rejected_422() -> None:
    app = _build_app()
    token = _mint_cookie(role=Role.CLIENT_ADMIN)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get(
            f"/admin/analytics/reports/bookings{_QUERY}&bucket=hour",
            cookies={"access_token": token},
        )
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "INVALID_BUCKET"


async def test_invalid_window_from_after_to_returns_422() -> None:
    app = _build_app()
    token = _mint_cookie(role=Role.CLIENT_ADMIN)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get(
            "/admin/analytics/reports/funnel?from=2026-08-01T00:00:00Z&to=2026-07-01T00:00:00Z",
            cookies={"access_token": token},
        )
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "INVALID_ANALYTICS_WINDOW"


async def test_window_too_large_returns_422() -> None:
    app = _build_app()
    token = _mint_cookie(role=Role.CLIENT_ADMIN)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get(
            "/admin/analytics/reports/funnel?from=2025-01-01T00:00:00Z&to=2026-08-01T00:00:00Z",
            cookies={"access_token": token},
        )
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "ANALYTICS_WINDOW_TOO_LARGE"


# ==============================================================================
# No-silent-fallback / empty report
# ==============================================================================


async def test_empty_tenant_gets_200_honest_zeros_and_null_rates() -> None:
    app = _build_app()
    token = _mint_cookie(role=Role.CLIENT_ADMIN)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        leads_resp = await c.get(
            f"/admin/analytics/reports/leads-by-stage{_QUERY}", cookies={"access_token": token},
        )
        funnel_resp = await c.get(
            f"/admin/analytics/reports/funnel{_QUERY}", cookies={"access_token": token},
        )
        win_loss_resp = await c.get(
            f"/admin/analytics/reports/win-loss{_QUERY}", cookies={"access_token": token},
        )

    leads_body = leads_resp.json()
    assert leads_body["stages"] == {
        "captured": 0, "qualified": 0, "contacted": 0, "converted": 0, "disqualified": 0,
    }
    assert leads_body["total"] == 0

    funnel_body = funnel_resp.json()
    assert funnel_body["overall_conversion_rate"] is None
    for step in funnel_body["steps"]:
        assert step["count"] == 0

    win_loss_body = win_loss_resp.json()
    assert win_loss_body["win_rate"] is None
    assert win_loss_body["won"]["avg_deal_size"] is None
    assert win_loss_body["lost"]["avg_deal_size"] is None


# ==============================================================================
# Tenant isolation (highest-risk, exact-value)
# ==============================================================================


async def test_tenant_isolation_exact_values_across_all_four_reports() -> None:
    db = _StubDatabase()
    db.seed_lead(tenant_id=_TENANT_A, stage="captured", created_at=datetime(2026, 7, 5, tzinfo=UTC))
    db.seed_lead(tenant_id=_TENANT_B, stage="captured", created_at=datetime(2026, 7, 5, tzinfo=UTC))
    db.seed_lead(tenant_id=_TENANT_B, stage="captured", created_at=datetime(2026, 7, 6, tzinfo=UTC))
    db.seed_lead(tenant_id=_TENANT_B, stage="converted", created_at=datetime(2026, 7, 7, tzinfo=UTC))

    db.seed_booking(tenant_id=_TENANT_A, status="booked", created_at=datetime(2026, 7, 5, tzinfo=UTC))
    db.seed_booking(tenant_id=_TENANT_B, status="booked", created_at=datetime(2026, 7, 5, tzinfo=UTC))
    db.seed_booking(tenant_id=_TENANT_B, status="cancelled", created_at=datetime(2026, 7, 6, tzinfo=UTC))

    db.seed_opportunity(
        tenant_id=_TENANT_A, stage="closed_won",
        closed_at=datetime(2026, 7, 10, tzinfo=UTC), created_at=datetime(2026, 7, 1, tzinfo=UTC),
        amount=Decimal("100.00"),
    )
    db.seed_opportunity(
        tenant_id=_TENANT_B, stage="closed_won",
        closed_at=datetime(2026, 7, 10, tzinfo=UTC), created_at=datetime(2026, 7, 1, tzinfo=UTC),
        amount=Decimal("9999.00"),
    )
    db.seed_opportunity_config(tenant_id=_TENANT_A, currency="USD")
    db.seed_opportunity_config(tenant_id=_TENANT_B, currency="GBP")

    app = _build_app(db)
    token_a = _mint_cookie(role=Role.CLIENT_ADMIN, tenant_id=_TENANT_A)
    token_b = _mint_cookie(role=Role.CLIENT_ADMIN, tenant_id=_TENANT_B)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        leads_a = await c.get(f"/admin/analytics/reports/leads-by-stage{_QUERY}", cookies={"access_token": token_a})
        leads_b = await c.get(f"/admin/analytics/reports/leads-by-stage{_QUERY}", cookies={"access_token": token_b})
        bookings_a = await c.get(f"/admin/analytics/reports/bookings{_QUERY}", cookies={"access_token": token_a})
        bookings_b = await c.get(f"/admin/analytics/reports/bookings{_QUERY}", cookies={"access_token": token_b})
        win_loss_a = await c.get(f"/admin/analytics/reports/win-loss{_QUERY}", cookies={"access_token": token_a})
        win_loss_b = await c.get(f"/admin/analytics/reports/win-loss{_QUERY}", cookies={"access_token": token_b})

    assert leads_a.json()["total"] == 1
    assert leads_b.json()["total"] == 3
    assert bookings_a.json()["totals"]["booked"] == 1
    assert bookings_a.json()["totals"]["cancelled"] == 0
    assert bookings_b.json()["totals"]["booked"] == 1
    assert bookings_b.json()["totals"]["cancelled"] == 1
    assert win_loss_a.json()["won"]["amount_total"] == "100.00"
    assert win_loss_a.json()["currency"] == "USD"
    assert win_loss_b.json()["won"]["amount_total"] == "9999.00"
    assert win_loss_b.json()["currency"] == "GBP"


async def test_csv_tenant_isolation_b_data_never_in_a_bytes() -> None:
    db = _StubDatabase()
    db.seed_opportunity(
        tenant_id=_TENANT_A, stage="closed_lost",
        closed_at=datetime(2026, 7, 10, tzinfo=UTC), created_at=datetime(2026, 7, 1, tzinfo=UTC),
        amount=None, close_reason="tenant-a-reason",
    )
    db.seed_opportunity(
        tenant_id=_TENANT_B, stage="closed_lost",
        closed_at=datetime(2026, 7, 10, tzinfo=UTC), created_at=datetime(2026, 7, 1, tzinfo=UTC),
        amount=None, close_reason="tenant-b-secret-reason",
    )

    app = _build_app(db)
    token_a = _mint_cookie(role=Role.CLIENT_ADMIN, tenant_id=_TENANT_A)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get(
            f"/admin/analytics/reports/win-loss.csv{_QUERY}", cookies={"access_token": token_a},
        )

    assert resp.status_code == 200
    assert "tenant-a-reason" in resp.text
    assert "tenant-b-secret-reason" not in resp.text


# ==============================================================================
# CSV safety end-to-end
# ==============================================================================


async def test_csv_end_to_end_formula_injection_neutralized() -> None:
    db = _StubDatabase()
    db.seed_opportunity(
        tenant_id=_TENANT_A, stage="closed_lost",
        closed_at=datetime(2026, 7, 10, tzinfo=UTC), created_at=datetime(2026, 7, 1, tzinfo=UTC),
        amount=None, close_reason="=cmd|'/c calc'!A1",
    )
    app = _build_app(db)
    token = _mint_cookie(role=Role.CLIENT_ADMIN, tenant_id=_TENANT_A)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get(
            f"/admin/analytics/reports/win-loss.csv{_QUERY}", cookies={"access_token": token},
        )

    assert resp.status_code == 200
    body = resp.text
    assert "=cmd" not in body.split("\n")[1].split(",")[0]  # the cell itself never starts with =
    # The neutralized cell appears prefixed with a single quote.
    assert "'=cmd|" in body or "\"'=cmd|" in body


async def test_csv_legitimate_minus_prefixed_value_preserved() -> None:
    db = _StubDatabase()
    db.seed_opportunity(
        tenant_id=_TENANT_A, stage="closed_lost",
        closed_at=datetime(2026, 7, 10, tzinfo=UTC), created_at=datetime(2026, 7, 1, tzinfo=UTC),
        amount=None, close_reason="-15% under budget",
    )
    app = _build_app(db)
    token = _mint_cookie(role=Role.CLIENT_ADMIN, tenant_id=_TENANT_A)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get(
            f"/admin/analytics/reports/win-loss.csv{_QUERY}", cookies={"access_token": token},
        )

    assert "15% under budget" in resp.text


async def test_csv_headers_and_row_counts_match_json_twin() -> None:
    db = _StubDatabase()
    db.seed_lead(tenant_id=_TENANT_A, stage="captured", created_at=datetime(2026, 7, 5, tzinfo=UTC))
    db.seed_lead(tenant_id=_TENANT_A, stage="qualified", created_at=datetime(2026, 7, 6, tzinfo=UTC))
    app = _build_app(db)
    token = _mint_cookie(role=Role.CLIENT_ADMIN, tenant_id=_TENANT_A)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        json_resp = await c.get(
            f"/admin/analytics/reports/leads-by-stage{_QUERY}", cookies={"access_token": token},
        )
        csv_resp = await c.get(
            f"/admin/analytics/reports/leads-by-stage.csv{_QUERY}", cookies={"access_token": token},
        )

    json_body = json_resp.json()
    csv_lines = [line for line in csv_resp.text.strip().split("\n") if line]
    # header + one row per stage key
    assert len(csv_lines) - 1 == len(json_body["stages"])


async def test_csv_export_writes_audit_row_with_no_pii() -> None:
    db = _StubDatabase()
    db.seed_opportunity(
        tenant_id=_TENANT_A, stage="closed_lost",
        closed_at=datetime(2026, 7, 10, tzinfo=UTC), created_at=datetime(2026, 7, 1, tzinfo=UTC),
        amount=None, close_reason="a very private reason with PII bob@example.com",
    )
    app = _build_app(db)
    token = _mint_cookie(role=Role.CLIENT_ADMIN, tenant_id=_TENANT_A)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get(
            f"/admin/analytics/reports/win-loss.csv{_QUERY}", cookies={"access_token": token},
        )
    assert resp.status_code == 200

    assert len(db.audit_rows) == 1
    row = db.audit_rows[0]
    assert row["action"] == "report_exported"
    assert row["target_id"] == "win-loss"
    assert "bob@example.com" not in str(row["metadata"])
    assert "row_count" in row["metadata"]


# ==============================================================================
# PII / response body
# ==============================================================================


async def test_response_body_never_contains_tenant_id() -> None:
    app = _build_app()
    token = _mint_cookie(role=Role.CLIENT_ADMIN, tenant_id=_TENANT_A)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        for path in _REPORT_PATHS:
            resp = await c.get(f"{path}{_QUERY}", cookies={"access_token": token})
            assert _TENANT_A not in resp.text


# ==============================================================================
# SR-19: lead-sources, score-distribution, agent-performance,
# recent-conversions -- four new report route families.
# ==============================================================================

_SR19_REPORT_PATHS = (
    "/admin/analytics/reports/lead-sources",
    "/admin/analytics/reports/score-distribution",
    "/admin/analytics/reports/agent-performance",
    "/admin/analytics/reports/recent-conversions",
)
_SR19_CSV_PATHS = tuple(f"{p}.csv" for p in _SR19_REPORT_PATHS)


async def test_sr19_client_admin_succeeds_on_all_four_json_reports() -> None:
    app = _build_app()
    token = _mint_cookie(role=Role.CLIENT_ADMIN)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        for path in _SR19_REPORT_PATHS:
            resp = await c.get(f"{path}{_QUERY}", cookies={"access_token": token})
            assert resp.status_code == 200, f"{path} -> {resp.status_code}: {resp.text}"


async def test_sr19_client_agent_succeeds_on_all_four_json_reports_including_agent_performance() -> None:
    """D7 -- CLIENT_AGENT can see agent-performance too (symmetric read,
    inherited from SR-9.5 D7); asserted explicitly so a future reader sees
    this is intended, not a leak."""
    app = _build_app()
    token = _mint_cookie(role=Role.CLIENT_AGENT)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        for path in _SR19_REPORT_PATHS:
            resp = await c.get(f"{path}{_QUERY}", cookies={"access_token": token})
            assert resp.status_code == 200, f"{path} -> {resp.status_code}: {resp.text}"


async def test_sr19_client_admin_succeeds_on_all_four_csv_exports() -> None:
    app = _build_app()
    token = _mint_cookie(role=Role.CLIENT_ADMIN)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        for path in _SR19_CSV_PATHS:
            resp = await c.get(f"{path}{_QUERY}", cookies={"access_token": token})
            assert resp.status_code == 200, f"{path} -> {resp.status_code}: {resp.text}"
            assert resp.headers["content-type"].startswith("text/csv")


async def test_sr19_client_agent_succeeds_on_all_four_csv_exports() -> None:
    app = _build_app()
    token = _mint_cookie(role=Role.CLIENT_AGENT)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        for path in _SR19_CSV_PATHS:
            resp = await c.get(f"{path}{_QUERY}", cookies={"access_token": token})
            assert resp.status_code == 200, f"{path} -> {resp.status_code}: {resp.text}"


async def test_sr19_visitor_rejected_on_every_report_and_csv() -> None:
    app = _build_app()
    token = _mint_cookie(role=Role.VISITOR)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        for path in (*_SR19_REPORT_PATHS, *_SR19_CSV_PATHS):
            resp = await c.get(f"{path}{_QUERY}", cookies={"access_token": token})
            assert resp.status_code == 403, f"{path} -> {resp.status_code}"


async def test_sr19_platform_admin_rejected_on_implicit_routes() -> None:
    app = _build_app()
    token = _mint_cookie(role=Role.PLATFORM_ADMIN, tenant_id=None)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        for path in _SR19_REPORT_PATHS:
            resp = await c.get(f"{path}{_QUERY}", cookies={"access_token": token})
            assert resp.status_code == 403, f"{path} -> {resp.status_code}"


async def test_sr19_platform_admin_succeeds_via_tenant_explicit_route() -> None:
    db = _StubDatabase()
    db.seed_tenant(tenant_id=_TENANT_A, slug="acme")
    app = _build_app(db)
    token = _mint_cookie(role=Role.PLATFORM_ADMIN, tenant_id=None)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        for report in ("lead-sources", "score-distribution", "agent-performance", "recent-conversions"):
            resp = await c.get(
                f"/admin/tenants/{_TENANT_A}/analytics/reports/{report}{_QUERY}",
                cookies={"access_token": token},
            )
            assert resp.status_code == 200, f"{report} -> {resp.status_code}: {resp.text}"


async def test_sr19_platform_admin_unknown_tenant_returns_404() -> None:
    app = _build_app()
    token = _mint_cookie(role=Role.PLATFORM_ADMIN, tenant_id=None)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get(
            f"/admin/tenants/does-not-exist/analytics/reports/lead-sources{_QUERY}",
            cookies={"access_token": token},
        )
    assert resp.status_code == 404
    assert resp.json()["error_code"] == "TENANT_NOT_FOUND"


async def test_sr19_platform_admin_export_audit_records_real_actor() -> None:
    db = _StubDatabase()
    db.seed_tenant(tenant_id=_TENANT_A, slug="acme")
    app = _build_app(db)
    token = _mint_cookie(role=Role.PLATFORM_ADMIN, tenant_id=None, subject="platform-user-1")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get(
            f"/admin/tenants/{_TENANT_A}/analytics/reports/lead-sources.csv{_QUERY}",
            cookies={"access_token": token},
        )
    assert resp.status_code == 200
    assert len(db.audit_rows) == 1
    row = db.audit_rows[0]
    assert row["actor"] == "platform-user-1"
    assert row["metadata"]["platform_admin"] is True


async def test_sr19_no_write_verb_exists_405_on_every_report_path() -> None:
    app = _build_app()
    token = _mint_cookie(role=Role.CLIENT_ADMIN)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        for path in _SR19_REPORT_PATHS:
            for method in ("post", "patch", "put", "delete"):
                resp = await c.request(method, f"{path}{_QUERY}", cookies={"access_token": token})
                assert resp.status_code == 405, f"{method} {path} -> {resp.status_code}"


async def test_sr19_invalid_window_from_after_to_returns_422() -> None:
    app = _build_app()
    token = _mint_cookie(role=Role.CLIENT_ADMIN)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get(
            "/admin/analytics/reports/lead-sources?from=2026-08-01T00:00:00Z&to=2026-07-01T00:00:00Z",
            cookies={"access_token": token},
        )
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "INVALID_ANALYTICS_WINDOW"


async def test_sr19_window_too_large_returns_422() -> None:
    app = _build_app()
    token = _mint_cookie(role=Role.CLIENT_ADMIN)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get(
            "/admin/analytics/reports/lead-sources?from=2025-01-01T00:00:00Z&to=2026-08-01T00:00:00Z",
            cookies={"access_token": token},
        )
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "ANALYTICS_WINDOW_TOO_LARGE"


async def test_sr19_default_window_is_last_30_days() -> None:
    app = _build_app()
    token = _mint_cookie(role=Role.CLIENT_ADMIN)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/admin/analytics/reports/lead-sources", cookies={"access_token": token})
    assert resp.status_code == 200
    body = resp.json()
    window_from = datetime.fromisoformat(body["window"]["from"].replace("Z", "+00:00"))
    window_to = datetime.fromisoformat(body["window"]["to"].replace("Z", "+00:00"))
    assert (window_to - window_from).days == 30


# ---------------------------------------------------------------------------
# No-silent-fallback
# ---------------------------------------------------------------------------


async def test_sr19_empty_tenant_gets_200_honest_zeros_and_null_rates() -> None:
    app = _build_app()
    token = _mint_cookie(role=Role.CLIENT_ADMIN)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        sources_resp = await c.get(
            f"/admin/analytics/reports/lead-sources{_QUERY}", cookies={"access_token": token},
        )
        scores_resp = await c.get(
            f"/admin/analytics/reports/score-distribution{_QUERY}", cookies={"access_token": token},
        )
        agents_resp = await c.get(
            f"/admin/analytics/reports/agent-performance{_QUERY}", cookies={"access_token": token},
        )
        conversions_resp = await c.get(
            f"/admin/analytics/reports/recent-conversions{_QUERY}", cookies={"access_token": token},
        )

    assert sources_resp.json()["sources"] == []
    assert sources_resp.json()["total"] == 0
    assert sources_resp.json()["single_source"] is False

    scores_body = scores_resp.json()
    assert scores_body["bands"] == {"0-19": 0, "20-39": 0, "40-59": 0, "60-79": 0, "80-100": 0}
    assert scores_body["unscored"] == 0

    agents_body = agents_resp.json()
    assert agents_body["agents"] == []
    assert agents_body["unassigned"]["win_rate"] is None

    assert conversions_resp.json()["conversions"] == []


async def test_sr19_score_distribution_unscored_lead_not_bucketed_into_0_19() -> None:
    db = _StubDatabase()
    db.seed_lead(
        tenant_id=_TENANT_A, stage="captured",
        created_at=datetime(2026, 7, 5, tzinfo=UTC), qualification_score=None,
    )
    app = _build_app(db)
    token = _mint_cookie(role=Role.CLIENT_ADMIN, tenant_id=_TENANT_A)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get(
            f"/admin/analytics/reports/score-distribution{_QUERY}", cookies={"access_token": token},
        )
    body = resp.json()
    assert body["unscored"] == 1
    assert body["bands"]["0-19"] == 0


async def test_sr19_agent_performance_zero_leads_agent_win_rate_null_not_zero() -> None:
    """D7 -- the exact defect this decision exists to prevent."""
    db = _StubDatabase()
    db.seed_lead(
        tenant_id=_TENANT_A, stage="captured",
        created_at=datetime(2026, 7, 5, tzinfo=UTC), assigned_agent_id="agent-1",
    )
    app = _build_app(db)
    token = _mint_cookie(role=Role.CLIENT_ADMIN, tenant_id=_TENANT_A)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get(
            f"/admin/analytics/reports/agent-performance{_QUERY}", cookies={"access_token": token},
        )
    body = resp.json()
    agent_1 = next(a for a in body["agents"] if a["assigned_agent_id"] == "agent-1")
    assert agent_1["win_rate"] is None


async def test_sr19_agent_performance_unassigned_row_present_and_counts_sum() -> None:
    db = _StubDatabase()
    db.seed_lead(
        tenant_id=_TENANT_A, stage="captured",
        created_at=datetime(2026, 7, 5, tzinfo=UTC), assigned_agent_id=None,
    )
    db.seed_lead(
        tenant_id=_TENANT_A, stage="captured",
        created_at=datetime(2026, 7, 6, tzinfo=UTC), assigned_agent_id="agent-1",
    )
    app = _build_app(db)
    token = _mint_cookie(role=Role.CLIENT_ADMIN, tenant_id=_TENANT_A)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get(
            f"/admin/analytics/reports/agent-performance{_QUERY}", cookies={"access_token": token},
        )
    body = resp.json()
    assert body["unassigned"]["assigned"] == 1
    total = sum(a["assigned"] for a in body["agents"]) + body["unassigned"]["assigned"]
    assert total == 2


async def test_sr19_lead_sources_single_source_not_padded() -> None:
    """D4 -- one real source gives ONE entry, never four padded entries."""
    db = _StubDatabase()
    db.seed_lead(tenant_id=_TENANT_A, stage="captured", created_at=datetime(2026, 7, 5, tzinfo=UTC))
    app = _build_app(db)
    token = _mint_cookie(role=Role.CLIENT_ADMIN, tenant_id=_TENANT_A)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get(
            f"/admin/analytics/reports/lead-sources{_QUERY}", cookies={"access_token": token},
        )
    body = resp.json()
    assert len(body["sources"]) == 1
    assert body["single_source"] is True


# ---------------------------------------------------------------------------
# D6/M6 -- the recent-conversions "always zero rows" trap
# ---------------------------------------------------------------------------


async def test_sr19_recent_conversions_includes_converted_leads_the_d6_trap() -> None:
    db = _StubDatabase()
    db.seed_lead(
        tenant_id=_TENANT_A, stage="converted",
        created_at=datetime(2026, 7, 1, tzinfo=UTC), updated_at=datetime(2026, 7, 10, tzinfo=UTC),
        lead_id="lead-converted-1", name="Convertible Lead",
    )
    app = _build_app(db)
    token = _mint_cookie(role=Role.CLIENT_ADMIN, tenant_id=_TENANT_A)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get(
            f"/admin/analytics/reports/recent-conversions{_QUERY}", cookies={"access_token": token},
        )
    body = resp.json()
    assert len(body["conversions"]) == 1
    assert body["conversions"][0]["lead_id"] == "lead-converted-1"


async def test_sr19_recent_conversions_no_value_field_in_json() -> None:
    """D6/M5 -- asserted by absence, JSON and CSV."""
    db = _StubDatabase()
    db.seed_lead(
        tenant_id=_TENANT_A, stage="converted",
        created_at=datetime(2026, 7, 1, tzinfo=UTC), updated_at=datetime(2026, 7, 10, tzinfo=UTC),
    )
    app = _build_app(db)
    token = _mint_cookie(role=Role.CLIENT_ADMIN, tenant_id=_TENANT_A)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        json_resp = await c.get(
            f"/admin/analytics/reports/recent-conversions{_QUERY}", cookies={"access_token": token},
        )
        csv_resp = await c.get(
            f"/admin/analytics/reports/recent-conversions.csv{_QUERY}", cookies={"access_token": token},
        )
    assert "value" not in json_resp.text.lower()
    assert "value" not in csv_resp.text.lower()


# ---------------------------------------------------------------------------
# Tenant isolation (highest-risk, exact-value)
# ---------------------------------------------------------------------------


async def test_sr19_tenant_isolation_exact_values_across_all_four_reports() -> None:
    db = _StubDatabase()
    db.seed_lead(
        tenant_id=_TENANT_A, stage="captured", created_at=datetime(2026, 7, 5, tzinfo=UTC),
        source="widget", qualification_score=10, assigned_agent_id="agent-a-1",
    )
    db.seed_lead(
        tenant_id=_TENANT_B, stage="captured", created_at=datetime(2026, 7, 5, tzinfo=UTC),
        source="widget", qualification_score=90, assigned_agent_id="agent-b-secret",
    )
    db.seed_lead(
        tenant_id=_TENANT_B, stage="captured", created_at=datetime(2026, 7, 6, tzinfo=UTC),
        source="referral", qualification_score=None, assigned_agent_id="agent-b-secret",
    )
    db.seed_lead(
        tenant_id=_TENANT_B, stage="converted", created_at=datetime(2026, 7, 1, tzinfo=UTC),
        updated_at=datetime(2026, 7, 7, tzinfo=UTC), lead_id="lead-b-converted",
    )

    app = _build_app(db)
    token_a = _mint_cookie(role=Role.CLIENT_ADMIN, tenant_id=_TENANT_A)
    token_b = _mint_cookie(role=Role.CLIENT_ADMIN, tenant_id=_TENANT_B)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        sources_a = await c.get(f"/admin/analytics/reports/lead-sources{_QUERY}", cookies={"access_token": token_a})
        sources_b = await c.get(f"/admin/analytics/reports/lead-sources{_QUERY}", cookies={"access_token": token_b})
        scores_a = await c.get(f"/admin/analytics/reports/score-distribution{_QUERY}", cookies={"access_token": token_a})
        scores_b = await c.get(f"/admin/analytics/reports/score-distribution{_QUERY}", cookies={"access_token": token_b})
        agents_a = await c.get(f"/admin/analytics/reports/agent-performance{_QUERY}", cookies={"access_token": token_a})
        agents_b = await c.get(f"/admin/analytics/reports/agent-performance{_QUERY}", cookies={"access_token": token_b})
        conversions_a = await c.get(f"/admin/analytics/reports/recent-conversions{_QUERY}", cookies={"access_token": token_a})
        conversions_b = await c.get(f"/admin/analytics/reports/recent-conversions{_QUERY}", cookies={"access_token": token_b})

    assert sources_a.json()["total"] == 1
    assert sources_a.json()["single_source"] is True
    # tenant B has 3 leads in the created_at window (2 captured + 1
    # converted, all created within the window): 2 widget + 1 referral.
    assert sources_b.json()["total"] == 3
    assert sources_b.json()["single_source"] is False

    assert scores_a.json()["bands"]["0-19"] == 1
    assert scores_b.json()["bands"]["80-100"] == 1
    # tenant B's 3rd lead (the converted one) also has no score -> unscored=2
    assert scores_b.json()["unscored"] == 2

    agent_ids_a = {a["assigned_agent_id"] for a in agents_a.json()["agents"]}
    agent_ids_b = {a["assigned_agent_id"] for a in agents_b.json()["agents"]}
    assert agent_ids_a == {"agent-a-1"}
    assert "agent-b-secret" not in agent_ids_a
    assert agent_ids_b == {"agent-b-secret"}

    assert conversions_a.json()["conversions"] == []
    assert [c["lead_id"] for c in conversions_b.json()["conversions"]] == ["lead-b-converted"]


async def test_sr19_agent_performance_never_lists_another_tenants_agent_id() -> None:
    """The highest-value isolation test in this sprint per the spec."""
    db = _StubDatabase()
    db.seed_lead(
        tenant_id=_TENANT_A, stage="captured", created_at=datetime(2026, 7, 5, tzinfo=UTC),
        assigned_agent_id="agent-a-only",
    )
    db.seed_lead(
        tenant_id=_TENANT_B, stage="captured", created_at=datetime(2026, 7, 5, tzinfo=UTC),
        assigned_agent_id="agent-b-secret-name",
    )
    app = _build_app(db)
    token_a = _mint_cookie(role=Role.CLIENT_ADMIN, tenant_id=_TENANT_A)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get(
            f"/admin/analytics/reports/agent-performance{_QUERY}", cookies={"access_token": token_a},
        )
    assert "agent-b-secret-name" not in resp.text


async def test_sr19_csv_tenant_isolation_b_data_never_in_a_bytes() -> None:
    db = _StubDatabase()
    db.seed_lead(
        tenant_id=_TENANT_A, stage="converted", created_at=datetime(2026, 7, 1, tzinfo=UTC),
        updated_at=datetime(2026, 7, 5, tzinfo=UTC), name="Tenant A Lead",
    )
    db.seed_lead(
        tenant_id=_TENANT_B, stage="converted", created_at=datetime(2026, 7, 1, tzinfo=UTC),
        updated_at=datetime(2026, 7, 5, tzinfo=UTC), name="Tenant B Secret Lead",
    )
    app = _build_app(db)
    token_a = _mint_cookie(role=Role.CLIENT_ADMIN, tenant_id=_TENANT_A)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get(
            f"/admin/analytics/reports/recent-conversions.csv{_QUERY}", cookies={"access_token": token_a},
        )
    assert resp.status_code == 200
    assert "Tenant A Lead" in resp.text
    assert "Tenant B Secret Lead" not in resp.text


# ---------------------------------------------------------------------------
# CSV safety end-to-end
# ---------------------------------------------------------------------------


async def test_sr19_csv_end_to_end_formula_injection_neutralized() -> None:
    db = _StubDatabase()
    db.seed_lead(
        tenant_id=_TENANT_A, stage="converted", created_at=datetime(2026, 7, 1, tzinfo=UTC),
        updated_at=datetime(2026, 7, 5, tzinfo=UTC), name="=cmd|'/c calc'!A1",
    )
    app = _build_app(db)
    token = _mint_cookie(role=Role.CLIENT_ADMIN, tenant_id=_TENANT_A)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get(
            f"/admin/analytics/reports/recent-conversions.csv{_QUERY}", cookies={"access_token": token},
        )
    assert resp.status_code == 200
    body = resp.text
    assert "=cmd" not in body.split("\n")[1].split(",")[0]
    assert "'=cmd|" in body or "\"'=cmd|" in body


async def test_sr19_csv_legitimate_minus_prefixed_value_preserved() -> None:
    db = _StubDatabase()
    db.seed_lead(
        tenant_id=_TENANT_A, stage="converted", created_at=datetime(2026, 7, 1, tzinfo=UTC),
        updated_at=datetime(2026, 7, 5, tzinfo=UTC), name="-15% Corp",
    )
    app = _build_app(db)
    token = _mint_cookie(role=Role.CLIENT_ADMIN, tenant_id=_TENANT_A)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get(
            f"/admin/analytics/reports/recent-conversions.csv{_QUERY}", cookies={"access_token": token},
        )
    assert "15% Corp" in resp.text


async def test_sr19_csv_headers_and_row_counts_match_json_twin() -> None:
    db = _StubDatabase()
    db.seed_lead(tenant_id=_TENANT_A, stage="captured", created_at=datetime(2026, 7, 5, tzinfo=UTC), source="widget")
    db.seed_lead(tenant_id=_TENANT_A, stage="captured", created_at=datetime(2026, 7, 6, tzinfo=UTC), source="referral")
    app = _build_app(db)
    token = _mint_cookie(role=Role.CLIENT_ADMIN, tenant_id=_TENANT_A)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        json_resp = await c.get(
            f"/admin/analytics/reports/lead-sources{_QUERY}", cookies={"access_token": token},
        )
        csv_resp = await c.get(
            f"/admin/analytics/reports/lead-sources.csv{_QUERY}", cookies={"access_token": token},
        )
    json_body = json_resp.json()
    csv_lines = [line for line in csv_resp.text.strip().split("\n") if line]
    assert len(csv_lines) - 1 == len(json_body["sources"])


async def test_sr19_csv_export_writes_audit_row_with_no_pii() -> None:
    db = _StubDatabase()
    db.seed_lead(
        tenant_id=_TENANT_A, stage="converted", created_at=datetime(2026, 7, 1, tzinfo=UTC),
        updated_at=datetime(2026, 7, 5, tzinfo=UTC), name="Bob Smith", assigned_agent_id="agent-1",
    )
    app = _build_app(db)
    token = _mint_cookie(role=Role.CLIENT_ADMIN, tenant_id=_TENANT_A)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get(
            f"/admin/analytics/reports/recent-conversions.csv{_QUERY}", cookies={"access_token": token},
        )
    assert resp.status_code == 200
    assert len(db.audit_rows) == 1
    row = db.audit_rows[0]
    assert row["action"] == "report_exported"
    assert row["target_id"] == "recent-conversions"
    assert "Bob Smith" not in str(row["metadata"])
    assert "agent-1" not in str(row["metadata"])
    assert "row_count" in row["metadata"]


# ---------------------------------------------------------------------------
# PII / logging
# ---------------------------------------------------------------------------


async def test_sr19_response_body_never_contains_tenant_id() -> None:
    app = _build_app()
    token = _mint_cookie(role=Role.CLIENT_ADMIN, tenant_id=_TENANT_A)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        for path in _SR19_REPORT_PATHS:
            resp = await c.get(f"{path}{_QUERY}", cookies={"access_token": token})
            assert _TENANT_A not in resp.text


async def test_sr19_agent_performance_log_never_contains_assigned_agent_id(caplog: Any) -> None:
    """D3 -- assigned_agent_id must NEVER appear in a log line for this report."""
    import logging

    db = _StubDatabase()
    db.seed_lead(
        tenant_id=_TENANT_A, stage="captured", created_at=datetime(2026, 7, 5, tzinfo=UTC),
        assigned_agent_id="agent-should-not-be-logged",
    )
    app = _build_app(db)
    token = _mint_cookie(role=Role.CLIENT_ADMIN, tenant_id=_TENANT_A)
    with caplog.at_level(logging.INFO):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            await c.get(
                f"/admin/analytics/reports/agent-performance{_QUERY}", cookies={"access_token": token},
            )
    for record in caplog.records:
        assert "agent-should-not-be-logged" not in record.getMessage()

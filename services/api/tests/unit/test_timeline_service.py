"""Unit tests for api.timeline.service.build_timeline (SR-9.3 D5/D6/D9/D11).

Mocks each of the four source-module fetch functions
(``_fetch_conversations``/``_fetch_lead_activities``/``_fetch_bookings``/
``_fetch_notifications``) directly so this suite tests the merge/sort/
truncate/degradation/pagination logic in isolation from any one source's
real SQL shape (that's covered by the repository-level tests).
"""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from common.auth import AuthClaims, Role

from api.timeline.identity import IdentitySet
from api.timeline.models import TimelineItem

_TEST_ENV = {
    "DEPLOYMENT_MODE": "saas",
    "DATABASE_URL": "postgres://stub-host:5432/appdb",
    "REDIS_URL": "redis://stub-host:6379",
    "JWT_SECRET": "x" * 48,
    "SECRET_ENCRYPTION_KEY": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    "SERVICE_NAME": "api",
    "LOG_LEVEL": "WARNING",
    "COOKIE_SECURE": "false",
}


def _reset_settings() -> None:
    from common.settings import get_settings

    from api.config import get_api_settings

    get_settings.cache_clear()
    get_api_settings.cache_clear()


def _claims() -> AuthClaims:
    return AuthClaims(subject="admin-1", role=Role.CLIENT_ADMIN, tenant_id="tenant-a")


def _item(kind: str, ts: datetime, item_id: str) -> TimelineItem:
    return TimelineItem(kind=kind, occurred_at=ts, item_id=item_id, data={})  # type: ignore[arg-type]


def _identities() -> IdentitySet:
    return IdentitySet(visitor_ids=("v1",), emails=("a@example.com",), lead_ids=("lead-1",))


class _StubDb:
    async def close(self) -> None:
        pass


async def test_build_timeline_interleaves_sources_by_occurred_at_desc() -> None:
    """Items from all four sources interleave in strict occurred_at DESC
    order -- a per-source-blocked result would fail this."""
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.timeline.service import build_timeline

        t = [datetime(2026, 1, 1, h, tzinfo=UTC) for h in range(1, 5)]
        with (
            patch(
                "api.timeline.service._fetch_conversations",
                return_value=([_item("message", t[3], "m1")], False),
            ),
            patch(
                "api.timeline.service._fetch_lead_activities",
                return_value=[_item("lead_activity", t[1], "a1")],
            ),
            patch(
                "api.timeline.service._fetch_bookings",
                return_value=[_item("booking", t[2], "b1")],
            ),
            patch(
                "api.timeline.service._fetch_notifications",
                return_value=[_item("notification", t[0], "n1")],
            ),
        ):
            result = await build_timeline(
                _StubDb(), _claims(), identities=_identities(), before=None, limit=50,
            )

        assert [i.item_id for i in result.items] == ["m1", "b1", "a1", "n1"]
        assert result.degraded is False


async def test_build_timeline_sources_block_always_present_on_success() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.timeline.service import build_timeline

        with (
            patch("api.timeline.service._fetch_conversations", return_value=([], False)),
            patch("api.timeline.service._fetch_lead_activities", return_value=[]),
            patch("api.timeline.service._fetch_bookings", return_value=[]),
            patch("api.timeline.service._fetch_notifications", return_value=[]),
        ):
            result = await build_timeline(
                _StubDb(), _claims(), identities=_identities(), before=None, limit=50,
            )

        assert set(result.sources.keys()) == {
            "conversations", "lead_activities", "bookings", "notifications",
        }
        for outcome in result.sources.values():
            assert outcome.state == "ok"
        assert result.degraded is False
        assert result.items == []


async def test_build_timeline_one_source_failing_degrades_others_present() -> None:
    """D6: a raised exception in one source -> that source unavailable,
    degraded=True, the other three sources' items still returned."""
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.timeline.service import build_timeline

        t = datetime(2026, 1, 1, tzinfo=UTC)
        with (
            patch(
                "api.timeline.service._fetch_conversations",
                return_value=([_item("message", t, "m1")], False),
            ),
            patch(
                "api.timeline.service._fetch_lead_activities",
                return_value=[_item("lead_activity", t, "a1")],
            ),
            patch(
                "api.timeline.service._fetch_bookings",
                return_value=[_item("booking", t, "b1")],
            ),
            patch(
                "api.timeline.service._fetch_notifications",
                side_effect=RuntimeError("db unavailable"),
            ),
        ):
            result = await build_timeline(
                _StubDb(), _claims(), identities=_identities(), before=None, limit=50,
            )

        assert result.degraded is True
        assert result.sources["notifications"].state == "unavailable"
        assert result.sources["conversations"].state == "ok"
        assert result.sources["lead_activities"].state == "ok"
        assert result.sources["bookings"].state == "ok"
        returned_ids = {i.item_id for i in result.items}
        assert returned_ids == {"m1", "a1", "b1"}


async def test_build_timeline_source_failure_logs_warning_no_pii(caplog: pytest.LogCaptureFixture) -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        import logging

        from api.timeline.service import build_timeline

        with (
            patch("api.timeline.service._fetch_conversations", return_value=([], False)),
            patch("api.timeline.service._fetch_lead_activities", return_value=[]),
            patch("api.timeline.service._fetch_bookings", return_value=[]),
            patch(
                "api.timeline.service._fetch_notifications",
                side_effect=RuntimeError("db unavailable: user alice@example.com"),
            ),
            caplog.at_level(logging.WARNING, logger="api.timeline.service"),
        ):
            await build_timeline(
                _StubDb(), _claims(), identities=_identities(), before=None, limit=50,
            )

        warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warning_records
        for record in warning_records:
            text = record.getMessage()
            assert "alice@example.com" not in text


async def test_build_timeline_empty_history_all_sources_ok_not_degraded() -> None:
    """Empty vs broken must be distinguishable: no history -> 200-eligible,
    items=[], all sources ok, degraded=False."""
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.timeline.service import build_timeline

        with (
            patch("api.timeline.service._fetch_conversations", return_value=([], False)),
            patch("api.timeline.service._fetch_lead_activities", return_value=[]),
            patch("api.timeline.service._fetch_bookings", return_value=[]),
            patch("api.timeline.service._fetch_notifications", return_value=[]),
        ):
            result = await build_timeline(
                _StubDb(),
                _claims(),
                identities=IdentitySet(visitor_ids=(), emails=(), lead_ids=()),
                before=None,
                limit=50,
            )

        assert result.items == []
        assert result.degraded is False
        assert all(o.state == "ok" for o in result.sources.values())


async def test_build_timeline_identical_timestamps_stable_tiebreaker_no_duplicate() -> None:
    """D9: two items sharing occurred_at order deterministically via the
    (occurred_at, kind, item_id) tiebreaker -- and appear exactly once."""
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.timeline.service import build_timeline

        t = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        with (
            patch(
                "api.timeline.service._fetch_conversations",
                return_value=([_item("message", t, "z-item")], False),
            ),
            patch(
                "api.timeline.service._fetch_lead_activities",
                return_value=[_item("lead_activity", t, "a-item")],
            ),
            patch("api.timeline.service._fetch_bookings", return_value=[]),
            patch("api.timeline.service._fetch_notifications", return_value=[]),
        ):
            result1 = await build_timeline(
                _StubDb(), _claims(), identities=_identities(), before=None, limit=50,
            )
            result2 = await build_timeline(
                _StubDb(), _claims(), identities=_identities(), before=None, limit=50,
            )

        ids_1 = [i.item_id for i in result1.items]
        ids_2 = [i.item_id for i in result2.items]
        assert ids_1 == ids_2  # deterministic ordering across repeated calls
        assert len(ids_1) == len(set(ids_1))  # no duplicates


async def test_build_timeline_limit_truncates_and_sets_next_before() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.timeline.service import build_timeline

        items = [
            _item("lead_activity", datetime(2026, 1, 1, h, tzinfo=UTC), f"a{h}")
            for h in range(1, 6)
        ]
        with (
            patch("api.timeline.service._fetch_conversations", return_value=([], False)),
            patch("api.timeline.service._fetch_lead_activities", return_value=items),
            patch("api.timeline.service._fetch_bookings", return_value=[]),
            patch("api.timeline.service._fetch_notifications", return_value=[]),
        ):
            result = await build_timeline(
                _StubDb(), _claims(), identities=_identities(), before=None, limit=2,
            )

        assert len(result.items) == 2
        assert result.items[0].item_id == "a5"
        assert result.items[1].item_id == "a4"
        assert result.next_before == result.items[-1].occurred_at


async def test_build_timeline_exhausted_next_before_is_null() -> None:
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.timeline.service import build_timeline

        items = [_item("lead_activity", datetime(2026, 1, 1, tzinfo=UTC), "only-one")]
        with (
            patch("api.timeline.service._fetch_conversations", return_value=([], False)),
            patch("api.timeline.service._fetch_lead_activities", return_value=items),
            patch("api.timeline.service._fetch_bookings", return_value=[]),
            patch("api.timeline.service._fetch_notifications", return_value=[]),
        ):
            result = await build_timeline(
                _StubDb(), _claims(), identities=_identities(), before=None, limit=50,
            )

        assert result.next_before is None


async def test_build_timeline_conversation_truncation_reported() -> None:
    """D11: a truncated conversation is reported on the conversations source."""
    with patch.dict("os.environ", _TEST_ENV, clear=False):
        _reset_settings()
        from api.timeline.service import build_timeline

        with (
            patch(
                "api.timeline.service._fetch_conversations",
                return_value=([_item("message", datetime(2026, 1, 1, tzinfo=UTC), "m1")], True),
            ),
            patch("api.timeline.service._fetch_lead_activities", return_value=[]),
            patch("api.timeline.service._fetch_bookings", return_value=[]),
            patch("api.timeline.service._fetch_notifications", return_value=[]),
        ):
            result = await build_timeline(
                _StubDb(), _claims(), identities=_identities(), before=None, limit=50,
            )

        assert result.sources["conversations"].truncated is True

"""Plan 2.1: no-show recovery -- a reschedule email now, one call the next business day (for
anyone who agreed to calls; owner 2026-11-18), a final email on day 3, then stop."""
from datetime import UTC, datetime, timedelta

import pytest

from api.agents import meetings


@pytest.mark.parametrize(("utc", "days"), [
    (datetime(2026, 11, 17, 15, tzinfo=UTC), 1),   # Tuesday -> Wednesday
    (datetime(2026, 11, 20, 15, tzinfo=UTC), 3),   # Friday -> Monday
    (datetime(2026, 11, 21, 15, tzinfo=UTC), 2),   # Saturday -> Monday
    (datetime(2026, 11, 22, 15, tzinfo=UTC), 1),   # Sunday -> Monday
    (datetime(2026, 11, 21, 2, tzinfo=UTC), 3),    # Saturday UTC, but 21:00 Friday in NY
])
def test_the_call_is_the_next_business_day_in_their_zone(utc: datetime, days: int) -> None:
    assert meetings.next_business_day(utc, "America/New_York") == timedelta(days=days)


def test_the_recovery_sequence() -> None:
    steps = meetings.no_show_steps(timedelta(days=1))
    assert [(s.channel, s.goal, s.offset) for s in steps] == [
        ("email", "re_engage", timedelta(0)),
        ("call", "book_meeting", timedelta(days=1)),
        ("email", "re_engage", timedelta(days=3)),
    ]
    assert [s.max_attempts for s in steps if s.channel == "call"] == [1]
    assert "weren't able to join" in steps[0].intent and "no pressure" in steps[0].intent
    assert "close out" in steps[1].intent
    assert "we stop" in steps[2].intent

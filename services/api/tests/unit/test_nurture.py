"""Plan 2.3: nurture timed to the start date they gave -- read from the points the client gave
that answer (so an edited question still works): top points = ready (no nurture), middle =
about a month, low = about three months, no answer = none."""
from datetime import timedelta

import pytest

from api.agents import nurture


@pytest.mark.parametrize(("points", "delay"), [
    (100, None),                 # "this month": they're ready, not a nurture lead
    (60, timedelta(days=30)),    # "in 1 to 3 months"
    (40, timedelta(days=30)),
    (39, timedelta(days=90)),
    (20, timedelta(days=90)),    # "later"
    (0, timedelta(days=90)),
    (None, None),                # never answered: no stated timeline, no nurture
])
def test_the_first_check_in_follows_their_timeline(points: int | None,
                                                   delay: timedelta | None) -> None:
    assert nurture.nurture_delay(points) == delay


def test_the_nurture_sequence_is_two_light_emails() -> None:
    steps = nurture.nurture_steps(timedelta(days=30))
    assert [(s.channel, s.goal, s.offset) for s in steps] == [
        ("email", "re_engage", timedelta(days=30)),
        ("email", "re_engage", timedelta(days=60)),
    ]
    assert "no pressure" in steps[0].intent.lower() and "for now" in steps[1].intent

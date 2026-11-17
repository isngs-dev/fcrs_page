"""Browsers still report legacy zone names (Chrome on Windows: "Asia/Calcutta"). Debian 13
images ship them only in tzdata-legacy, so without the ``tzdata`` package a chat lead's zone
was rejected and their call step dropped with CONTACT_TIMEZONE_UNKNOWN (demo, 2026-11-17)."""
from zoneinfo import ZoneInfo

import pytest


@pytest.mark.parametrize("zone", ["Asia/Calcutta", "Asia/Kolkata", "US/Eastern", "Asia/Saigon"])
def test_legacy_browser_zone_names_resolve(zone: str) -> None:
    assert ZoneInfo(zone).key == zone

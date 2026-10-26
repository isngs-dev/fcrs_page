"""A4 H2: channel consent is validated before anything is stored, and the stored
wording always comes from the server's registry for the version shown."""
from __future__ import annotations

import pytest
from common.errors import ValidationError

from api.agents.consent_wording import (
    CURRENT_VERSION,
    WORDING,
    ChannelConsentsIn,
    consent_record,
    granted_channels,
)


def test_no_boxes_means_no_channels() -> None:
    assert granted_channels(None, has_phone=True) == []
    assert granted_channels(ChannelConsentsIn(version=CURRENT_VERSION), has_phone=True) == []


def test_ticked_channels_in_order() -> None:
    boxes = ChannelConsentsIn(email=True, call=True, version=CURRENT_VERSION)
    assert granted_channels(boxes, has_phone=True) == ["email", "call"]


def test_unknown_version_is_rejected() -> None:
    with pytest.raises(ValidationError) as exc:
        granted_channels(ChannelConsentsIn(email=True, version="made-up"), has_phone=True)
    assert exc.value.code == "CONSENT_VERSION_UNKNOWN"


def test_call_consent_needs_a_phone() -> None:
    with pytest.raises(ValidationError) as exc:
        granted_channels(ChannelConsentsIn(call=True, version=CURRENT_VERSION), has_phone=False)
    assert exc.value.code == "CALL_CONSENT_NEEDS_PHONE"


def test_records_use_server_wording_and_bases() -> None:
    call = consent_record("call", CURRENT_VERSION)
    assert call == {"channel": "call", "basis": "express_written_consent",
                    "wording": WORDING[CURRENT_VERSION]["call"], "wording_version": CURRENT_VERSION}
    assert "AI assistant" in call["wording"] and "not a condition of purchase" in call["wording"]
    assert consent_record("email", CURRENT_VERSION)["basis"] == "express_consent"


def test_widget_mirror_matches_server_wording() -> None:
    """The widget displays its own copy; it must be the server's text for the version."""
    from pathlib import Path

    candidates = [p / "apps/widget/src/channelConsent.ts" for p in Path(__file__).resolve().parents]
    source = next((c for c in candidates if c.exists()), None)
    if source is None:
        pytest.skip("widget sources not present in this test environment")
    text = source.read_text(encoding="utf-8").replace('" +\n  "', "")
    assert f'CHANNEL_CONSENT_VERSION = "{CURRENT_VERSION}"' in text
    for channel in ("email", "call"):
        assert WORDING[CURRENT_VERSION][channel] in text

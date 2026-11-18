"""Plan 1.6: the signed "manage your booking" link. One purpose, one booking, one tenant --
a forged, altered or other-purpose token is refused."""
from __future__ import annotations

import pytest

from api.config import get_api_settings
from api.scheduling import manage


@pytest.fixture(autouse=True)
def _secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMAIL_LINK_SECRET", "s" * 40)
    monkeypatch.setenv("PUBLIC_API_BASE_URL", "https://api.example.com")
    get_api_settings.cache_clear()
    yield
    get_api_settings.cache_clear()


def test_a_token_round_trips_to_its_booking() -> None:
    token = manage.sign("t-1", "evt-1")
    assert manage.verify(token) == ("t-1", "evt-1")
    assert manage.manage_url("t-1", "evt-1") == (
        f"https://api.example.com/public/bookings/{token}")


def test_forged_altered_or_other_purpose_tokens_are_refused() -> None:
    from api.email_agent.links import sign_unsubscribe

    token = manage.sign("t-1", "evt-1")
    payload, mac = token.split(".")
    assert manage.verify(f"{payload}.{mac[:-2]}xx") is None
    other = manage.sign("t-2", "evt-1").split(".")[0]
    assert manage.verify(f"{other}.{mac}") is None  # another tenant's booking, same MAC
    assert manage.verify(sign_unsubscribe("t-1", "evt-1", "a@b.co")) is None
    assert manage.verify("nonsense") is None


def test_no_link_without_the_secret_or_a_public_address(monkeypatch: pytest.MonkeyPatch
                                                         ) -> None:
    monkeypatch.delenv("PUBLIC_API_BASE_URL")
    get_api_settings.cache_clear()
    assert manage.manage_url("t-1", "evt-1") is None

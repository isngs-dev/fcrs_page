"""URL settings are trimmed: a stray space in .env must not end up inside every
webhook URL the API hands to SendGrid/Plivo (and their signature checks)."""
from __future__ import annotations

import pytest

from api.config import ApiSettings

_REQUIRED = {
    "DEPLOYMENT_MODE": "saas",
    "DATABASE_URL": "postgres://stub-host:5432/appdb",
    "JWT_SECRET": "x" * 48,
    "SECRET_ENCRYPTION_KEY": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
}


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for key, value in _REQUIRED.items():
        monkeypatch.setenv(key, value)
    return monkeypatch


@pytest.mark.parametrize("field", ["PUBLIC_API_BASE_URL", "ADMIN_WEB_BASE_URL",
                                   "PASSWORD_RESET_URL_BASE", "GOOGLE_OAUTH_REDIRECT_URI"])
def test_url_settings_are_trimmed(env: pytest.MonkeyPatch, field: str) -> None:
    env.setenv(field, "  https://abc.example.com \t")
    settings = ApiSettings(_env_file=None)  # type: ignore[call-arg]
    assert getattr(settings, field.lower()) == "https://abc.example.com"


def test_blank_public_api_base_url_counts_as_unset(env: pytest.MonkeyPatch) -> None:
    env.setenv("PUBLIC_API_BASE_URL", "   ")
    assert ApiSettings(_env_file=None).public_api_base_url is None  # type: ignore[call-arg]

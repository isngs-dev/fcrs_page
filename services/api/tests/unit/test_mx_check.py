"""Unit tests for api.leads.mx_check (the DNS MX layer + its cache-aside wrapper).

``_check_mx_live`` is tested by injecting fake ``dns.resolver.resolve``
behavior (never a real network call). ``check_mx_cached`` is tested against
the real ``common.cache.InMemoryCache`` (a genuine, simple implementation of
the Cache protocol -- no need for a hand-rolled double).
"""
from __future__ import annotations

from unittest.mock import patch

import dns.resolver
from common.auth import AuthClaims, Role
from common.cache import InMemoryCache

from api.leads.mx_check import MX_CACHE_TTL_SECONDS, _check_mx_live, check_mx_cached

_CLAIMS = AuthClaims(subject="system:test", role=Role.CLIENT_ADMIN, tenant_id="tenant-mx-test")


# ==============================================================================
# _check_mx_live
# ==============================================================================


def test_mx_record_present_is_ok() -> None:
    with patch("api.leads.mx_check.dns.resolver.resolve", return_value=["mx.example.com"]):
        assert _check_mx_live("example.com", timeout_seconds=3) == "ok"


def test_nxdomain_is_absent() -> None:
    with patch(
        "api.leads.mx_check.dns.resolver.resolve",
        side_effect=dns.resolver.NXDOMAIN(),
    ):
        assert _check_mx_live("nonexistent.example", timeout_seconds=3) == "absent"


def test_no_mx_record_is_absent_never_falls_back_to_an_a_record_check() -> None:
    """Regression guard: a real-world bug this fixed. 'gmel.com' (a typo of
    gmail.com) has no MX record but DOES have an A record for a parking
    page -- an earlier version of this module implemented the RFC 5321
    implicit-MX-via-A fallback and treated that as 'ok', letting an
    obviously-fake lead through as qualified. No MX record -> absent, full
    stop; the A record is never even queried anymore (only one
    dns.resolver.resolve call happens per domain)."""

    def _raise_no_answer(_domain: str, rdtype: str, lifetime: float) -> list[str]:  # noqa: ARG001
        raise dns.resolver.NoAnswer()

    with patch(
        "api.leads.mx_check.dns.resolver.resolve", side_effect=_raise_no_answer,
    ) as mock_resolve:
        assert _check_mx_live("gmel.com", timeout_seconds=3) == "absent"

    mock_resolve.assert_called_once_with("gmel.com", "MX", lifetime=3)


def test_no_mx_and_no_a_record_is_absent() -> None:
    def _side_effect(_domain: str, rdtype: str, lifetime: float) -> list[str]:  # noqa: ARG001
        raise dns.resolver.NoAnswer()

    with patch("api.leads.mx_check.dns.resolver.resolve", side_effect=_side_effect):
        assert _check_mx_live("nothing-here.example", timeout_seconds=3) == "absent"


def test_timeout_is_error_never_absent() -> None:
    """The core ambiguous-case contract at the DNS layer: a timeout must
    never be reported as 'absent' (which would disqualify a possibly-good
    lead on a transient infra blip)."""
    with patch(
        "api.leads.mx_check.dns.resolver.resolve",
        side_effect=dns.resolver.Timeout(),
    ):
        assert _check_mx_live("gmail.com", timeout_seconds=3) == "error"


def test_no_nameservers_is_error() -> None:
    with patch(
        "api.leads.mx_check.dns.resolver.resolve",
        side_effect=dns.resolver.NoNameservers(),
    ):
        assert _check_mx_live("example.com", timeout_seconds=3) == "error"


def test_unexpected_exception_is_error_not_raised() -> None:
    """Never raises -- an unanticipated resolver failure still maps to a
    result, not an exception escaping into the caller."""
    with patch(
        "api.leads.mx_check.dns.resolver.resolve",
        side_effect=RuntimeError("unexpected resolver crash"),
    ):
        assert _check_mx_live("example.com", timeout_seconds=3) == "error"


# ==============================================================================
# check_mx_cached
# ==============================================================================


async def test_cache_miss_calls_live_check_and_populates_the_cache() -> None:
    cache = InMemoryCache()
    calls: list[str] = []

    def _fake_live(domain: str, *, timeout_seconds: float) -> str:  # noqa: ARG001
        calls.append(domain)
        return "ok"

    with patch("api.leads.mx_check._check_mx_live", side_effect=_fake_live):
        result = await check_mx_cached(cache, _CLAIMS, "example.com", timeout_seconds=3)

    assert result == "ok"
    assert calls == ["example.com"]


async def test_cache_hit_skips_the_live_check_entirely() -> None:
    cache = InMemoryCache()
    calls: list[str] = []

    def _fake_live(domain: str, *, timeout_seconds: float) -> str:  # noqa: ARG001
        calls.append(domain)
        return "ok"

    with patch("api.leads.mx_check._check_mx_live", side_effect=_fake_live):
        await check_mx_cached(cache, _CLAIMS, "example.com", timeout_seconds=3)  # populates cache
        result = await check_mx_cached(cache, _CLAIMS, "example.com", timeout_seconds=3)  # should hit cache

    assert result == "ok"
    assert calls == ["example.com"], "the second call must not re-invoke the live DNS check"


async def test_absent_result_is_cached_too() -> None:
    cache = InMemoryCache()

    with patch("api.leads.mx_check._check_mx_live", return_value="absent"):
        await check_mx_cached(cache, _CLAIMS, "bad-domain.example", timeout_seconds=3)

    key = f"tenant:{_CLAIMS.tenant_id}:email_mx:bad-domain.example"
    assert await cache.get(key) == "absent"


async def test_error_result_is_never_cached() -> None:
    """A transient DNS failure must never be remembered -- every subsequent
    lead with that domain gets a fresh live check, not a stale 'error'."""
    cache = InMemoryCache()
    calls = 0

    def _fake_live(domain: str, *, timeout_seconds: float) -> str:  # noqa: ARG001
        nonlocal calls
        calls += 1
        return "error"

    with patch("api.leads.mx_check._check_mx_live", side_effect=_fake_live):
        r1 = await check_mx_cached(cache, _CLAIMS, "flaky.example", timeout_seconds=3)
        r2 = await check_mx_cached(cache, _CLAIMS, "flaky.example", timeout_seconds=3)

    assert r1 == r2 == "error"
    assert calls == 2, "an 'error' outcome must never be cached, so every call re-checks live"


async def test_cache_key_is_tenant_scoped() -> None:
    """Two different tenants querying the same domain must not share a cache
    entry (CLAUDE.md §3: cache keys always include tenant_id)."""
    cache = InMemoryCache()
    other_claims = AuthClaims(subject="system:test", role=Role.CLIENT_ADMIN, tenant_id="tenant-other")

    with patch("api.leads.mx_check._check_mx_live", return_value="ok"):
        await check_mx_cached(cache, _CLAIMS, "shared-domain.example", timeout_seconds=3)

    key_for_other_tenant = f"tenant:{other_claims.tenant_id}:email_mx:shared-domain.example"
    assert await cache.get(key_for_other_tenant) is None


def test_ttl_is_a_full_day() -> None:
    assert MX_CACHE_TTL_SECONDS == 86_400

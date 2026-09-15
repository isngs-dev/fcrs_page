"""Unit tests for api.ingestion.url_safety (add-a-website feature).

No real network calls or real DNS resolution: ``socket.getaddrinfo`` is
mocked per test, and ``fetch_url_safely``'s HTTP layer is exercised via
``httpx.MockTransport`` (patched in place of the module's real
``httpx.AsyncClient`` construction).

Covers:
- normalize_url: scheme/host lowercasing, trailing-slash/fragment stripping.
- validate_url: accepts a public IP; rejects non-http(s) schemes, embedded
  credentials, a missing host, unresolvable hosts, and every disallowed IP
  class (loopback/private/link-local/multicast/reserved).
- fetch_url_safely: happy path; non-2xx final status; a safe URL redirecting
  to an unsafe one; too many redirects; the byte-size cap; a network error.
"""
from __future__ import annotations

import socket
from unittest.mock import patch

import httpx
import pytest
from common.errors import ValidationError

from api.ingestion.url_safety import fetch_url_safely, normalize_url, validate_url

_PUBLIC_IP = "93.184.216.34"  # example.com's real public IP -- any public addr works


def _addrinfo(ip: str) -> list[tuple]:
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    return [(family, socket.SOCK_STREAM, 6, "", (ip, 443))]


# ==============================================================================
# normalize_url
# ==============================================================================


def test_normalize_url_lowercases_scheme_and_host() -> None:
    # Only scheme+host are lowercased -- paths are case-sensitive on most
    # servers, so "Path" must NOT become "path".
    assert normalize_url("HTTPS://Example.COM/Path") == "https://example.com/Path"


def test_normalize_url_strips_trailing_slash() -> None:
    assert normalize_url("https://example.com/path/") == "https://example.com/path"


def test_normalize_url_strips_fragment() -> None:
    assert normalize_url("https://example.com/path#section") == "https://example.com/path"


def test_normalize_url_keeps_query() -> None:
    assert normalize_url("https://example.com/path?a=1") == "https://example.com/path?a=1"


def test_normalize_url_equivalent_urls_produce_the_same_key() -> None:
    assert normalize_url("https://Example.com/") == normalize_url("https://example.com")


# ==============================================================================
# validate_url
# ==============================================================================


def test_validate_url_accepts_a_public_https_url() -> None:
    with patch("api.ingestion.url_safety.socket.getaddrinfo", return_value=_addrinfo(_PUBLIC_IP)):
        validate_url("https://example.com/page")  # does not raise


@pytest.mark.parametrize("scheme", ["ftp", "file", "javascript", "data"])
def test_validate_url_rejects_non_http_schemes(scheme: str) -> None:
    with pytest.raises(ValidationError) as exc_info:
        validate_url(f"{scheme}://example.com/x")
    assert exc_info.value.code == "URL_NOT_ALLOWED"


def test_validate_url_rejects_embedded_credentials() -> None:
    with pytest.raises(ValidationError) as exc_info:
        validate_url("https://user:pass@example.com/")
    assert exc_info.value.code == "URL_NOT_ALLOWED"


def test_validate_url_rejects_missing_host() -> None:
    with pytest.raises(ValidationError) as exc_info:
        validate_url("https:///path-only")
    assert exc_info.value.code == "URL_NOT_ALLOWED"


def test_validate_url_rejects_unresolvable_host() -> None:
    with patch("api.ingestion.url_safety.socket.getaddrinfo", side_effect=OSError("no such host")):
        with pytest.raises(ValidationError) as exc_info:
            validate_url("https://does-not-exist.invalid/")
    assert exc_info.value.code == "URL_NOT_ALLOWED"


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",  # loopback
        "10.0.0.5",  # private
        "172.16.0.1",  # private
        "192.168.1.1",  # private
        "169.254.1.1",  # link-local
        "224.0.0.1",  # multicast
        "0.0.0.0",  # noqa: S104 — unspecified address, tested as a resolved IP not a bind
        "::1",  # IPv6 loopback
        "fe80::1",  # IPv6 link-local
        "fc00::1",  # IPv6 unique-local (is_private)
    ],
)
def test_validate_url_rejects_every_disallowed_ip_class(ip: str) -> None:
    with patch("api.ingestion.url_safety.socket.getaddrinfo", return_value=_addrinfo(ip)):
        with pytest.raises(ValidationError) as exc_info:
            validate_url("https://internal.example/")
    assert exc_info.value.code == "URL_NOT_ALLOWED"


def test_validate_url_rejects_if_any_resolved_address_is_unsafe() -> None:
    """Round-robin DNS: one safe + one unsafe address -- must reject, not
    just check the first answer."""
    mixed = _addrinfo(_PUBLIC_IP) + _addrinfo("127.0.0.1")
    with patch("api.ingestion.url_safety.socket.getaddrinfo", return_value=mixed):
        with pytest.raises(ValidationError) as exc_info:
            validate_url("https://mixed.example/")
    assert exc_info.value.code == "URL_NOT_ALLOWED"


# ==============================================================================
# fetch_url_safely
# ==============================================================================


def _patched_client(handler):  # type: ignore[no-untyped-def]
    """Patch api.ingestion.url_safety's httpx.AsyncClient construction to
    return a real AsyncClient wired to a MockTransport -- no real network."""
    return patch(
        "api.ingestion.url_safety.httpx.AsyncClient",
        return_value=httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False),
    )


async def test_fetch_url_safely_happy_path() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>hi</html>")

    with patch("api.ingestion.url_safety.socket.getaddrinfo", return_value=_addrinfo(_PUBLIC_IP)):
        with _patched_client(handler):
            result = await fetch_url_safely("https://example.com/", max_bytes=1000, timeout_seconds=5.0)

    assert result == b"<html>hi</html>"


async def test_fetch_url_safely_non_2xx_status_raises_fetch_failed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"error")

    with patch("api.ingestion.url_safety.socket.getaddrinfo", return_value=_addrinfo(_PUBLIC_IP)):
        with _patched_client(handler):
            with pytest.raises(ValidationError) as exc_info:
                await fetch_url_safely("https://example.com/", max_bytes=1000, timeout_seconds=5.0)

    assert exc_info.value.code == "URL_FETCH_FAILED"


async def test_fetch_url_safely_rejects_redirect_to_an_unsafe_host() -> None:
    """A SAFE URL that redirects to an internal address must be rejected --
    re-validated on the redirect hop, not just the original URL."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "safe.example":
            return httpx.Response(302, headers={"location": "https://internal.example/"})
        return httpx.Response(200, content=b"should never be reached")

    def addrinfo_side_effect(hostname: str, *args: object, **kwargs: object) -> list[tuple]:
        if hostname == "safe.example":
            return _addrinfo(_PUBLIC_IP)
        return _addrinfo("127.0.0.1")

    with patch("api.ingestion.url_safety.socket.getaddrinfo", side_effect=addrinfo_side_effect):
        with _patched_client(handler):
            with pytest.raises(ValidationError) as exc_info:
                await fetch_url_safely("https://safe.example/", max_bytes=1000, timeout_seconds=5.0)

    assert exc_info.value.code == "URL_NOT_ALLOWED"


async def test_fetch_url_safely_too_many_redirects_raises_fetch_failed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # Always redirect to itself -- exceeds the redirect cap.
        return httpx.Response(302, headers={"location": str(request.url)})

    with patch("api.ingestion.url_safety.socket.getaddrinfo", return_value=_addrinfo(_PUBLIC_IP)):
        with _patched_client(handler):
            with pytest.raises(ValidationError) as exc_info:
                await fetch_url_safely("https://example.com/", max_bytes=1000, timeout_seconds=5.0)

    assert exc_info.value.code == "URL_FETCH_FAILED"


async def test_fetch_url_safely_enforces_byte_cap() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 2000)

    with patch("api.ingestion.url_safety.socket.getaddrinfo", return_value=_addrinfo(_PUBLIC_IP)):
        with _patched_client(handler):
            with pytest.raises(ValidationError) as exc_info:
                await fetch_url_safely("https://example.com/", max_bytes=1000, timeout_seconds=5.0)

    assert exc_info.value.code == "URL_TOO_LARGE"


async def test_fetch_url_safely_network_error_raises_fetch_failed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with patch("api.ingestion.url_safety.socket.getaddrinfo", return_value=_addrinfo(_PUBLIC_IP)):
        with _patched_client(handler):
            with pytest.raises(ValidationError) as exc_info:
                await fetch_url_safely("https://example.com/", max_bytes=1000, timeout_seconds=5.0)

    assert exc_info.value.code == "URL_FETCH_FAILED"


async def test_fetch_url_safely_rejects_disallowed_scheme_before_any_fetch() -> None:
    with pytest.raises(ValidationError) as exc_info:
        await fetch_url_safely("ftp://example.com/", max_bytes=1000, timeout_seconds=5.0)
    assert exc_info.value.code == "URL_NOT_ALLOWED"

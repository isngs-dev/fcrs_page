"""SSRF-safe external URL fetching for the add-a-website ingestion source.

No safe-external-fetch pattern existed anywhere in this codebase before this
module (the closest analog, ``api.crm.sync.WebhookSync``, only has a
timeout -- no host/IP validation at all). A client-submitted URL that the
server fetches and derives searchable content from is a real SSRF vector
(cloud metadata endpoints, internal services, etc.), so ``fetch_url_safely``
validates the scheme, resolves and checks every DNS answer against the
private/loopback/link-local/reserved ranges, and re-validates on every
redirect hop (closes the "safe URL redirects to an internal one" gap) --
never trusting a single check up front.

Called from both the route (fast rejection at submission time) and again
inside the Celery task right before the real fetch (closes the
submission-to-worker-pickup DNS-rebinding TOCTOU gap).
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

import httpx
from common.errors import ValidationError

_ALLOWED_SCHEMES = {"http", "https"}
_MAX_REDIRECTS = 3


def normalize_url(url: str) -> str:
    """Lowercase scheme+host, strip a trailing slash and any fragment.

    Used both as the idempotency key (``content_hash = sha256(normalize_url
    (url))``) and to derive a stable synthetic filename -- so re-submitting
    "https://Example.com/" and "https://example.com" hit the same doc.
    """
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()
    path = parts.path.rstrip("/")
    normalized = f"{scheme}://{netloc}{path}"
    if parts.query:
        normalized += f"?{parts.query}"
    return normalized


def _reject_unsafe_host(hostname: str) -> None:
    """Resolve ``hostname`` and raise if ANY resolved address is unsafe.

    A hostname can resolve to multiple addresses (round-robin DNS); every
    one of them must be safe, not just the first.
    """
    try:
        infos = socket.getaddrinfo(hostname, None)
    except OSError as exc:
        raise ValidationError(
            f"Could not resolve host: {hostname!r}.",
            code="URL_NOT_ALLOWED",
        ) from exc

    for info in infos:
        raw_addr = str(info[4][0])
        addr = ipaddress.ip_address(raw_addr.split("%")[0])  # strip IPv6 zone id
        if (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_multicast
            or addr.is_reserved
            or addr.is_unspecified
        ):
            raise ValidationError(
                f"That URL points to a disallowed address ({raw_addr}).",
                code="URL_NOT_ALLOWED",
            )


def _validate_url_shape(url: str) -> str:
    """Scheme/host/credential checks that don't require a DNS lookup.

    Returns the hostname on success. Raises ``ValidationError
    (URL_NOT_ALLOWED)`` on a disallowed scheme, missing host, or embedded
    credentials (``user:pass@host`` -- never a legitimate use here).
    """
    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        raise ValidationError(
            f"Unsupported URL scheme: {parts.scheme!r}. Only http/https are allowed.",
            code="URL_NOT_ALLOWED",
        )
    if parts.username or parts.password:
        raise ValidationError(
            "URLs with embedded credentials are not allowed.",
            code="URL_NOT_ALLOWED",
        )
    if not parts.hostname:
        raise ValidationError("URL is missing a host.", code="URL_NOT_ALLOWED")
    return parts.hostname


def validate_url(url: str) -> None:
    """Full submission-time check (shape + DNS/IP) with no network fetch.

    Called by the route so an obviously-blocked URL is rejected immediately,
    before a doc/run row is even created -- not left to fail asynchronously
    in the worker.
    """
    hostname = _validate_url_shape(url)
    _reject_unsafe_host(hostname)


async def fetch_url_safely(
    url: str,
    *,
    max_bytes: int,
    timeout_seconds: float,
) -> bytes:
    """Fetch ``url``, re-validating scheme+DNS on every redirect hop.

    Streams the response body, aborting with ``ValidationError
    (URL_TOO_LARGE)`` once ``max_bytes`` is exceeded. Raises
    ``ValidationError(URL_FETCH_FAILED)`` on a network error, timeout, or
    non-2xx final response. These are all deterministic failures that the
    caller (the ingestion task) handles via its existing "mark run/doc
    failed, do not retry" path -- the same one ``parsers.parse`` errors
    already use.
    """
    current_url = url
    async with httpx.AsyncClient(follow_redirects=False, timeout=timeout_seconds) as client:
        for _hop in range(_MAX_REDIRECTS + 1):
            hostname = _validate_url_shape(current_url)
            _reject_unsafe_host(hostname)

            try:
                async with client.stream("GET", current_url) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise ValidationError(
                                "Redirect response had no Location header.",
                                code="URL_FETCH_FAILED",
                            )
                        current_url = str(response.url.join(location))
                        continue

                    if response.status_code < 200 or response.status_code >= 300:
                        raise ValidationError(
                            f"Fetching the URL returned status {response.status_code}.",
                            code="URL_FETCH_FAILED",
                        )

                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > max_bytes:
                            raise ValidationError(
                                f"Page exceeds the {max_bytes}-byte fetch limit.",
                                code="URL_TOO_LARGE",
                            )
                        chunks.append(chunk)
                    return b"".join(chunks)
            except httpx.HTTPError as exc:
                raise ValidationError(
                    f"Failed to fetch URL: {exc}.",
                    code="URL_FETCH_FAILED",
                ) from exc

    raise ValidationError(
        f"Too many redirects (limit {_MAX_REDIRECTS}).",
        code="URL_FETCH_FAILED",
    )

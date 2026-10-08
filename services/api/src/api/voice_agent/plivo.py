"""Plivo plumbing for the AI voice agent: webhook signatures, browser JWTs, XML.

No ``plivo`` package -- same raw-``httpx`` precedent as the Twilio code.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time
from urllib.parse import parse_qs, urlsplit
from xml.sax.saxutils import escape, quoteattr

import httpx
from common.errors import AuthenticationError
from fastapi import Response

PLIVO_API_BASE = "https://api.plivo.com/v1"
SIGNATURE_HEADER = "X-Plivo-Signature-V3"
NONCE_HEADER = "X-Plivo-Signature-V3-Nonce"


class PlivoSignatureInvalidError(AuthenticationError):
    """Missing/mismatched Plivo signature, or a webhook for no known call (401).
    One code for every rejection mode -- never leaks which check failed."""

    code = "PLIVO_SIGNATURE_INVALID"
    default_message = "The Plivo webhook signature is missing or invalid."


def _signed_string(url: str, params: dict[str, str]) -> str:
    """What Plivo signs for a POST -- mirrors plivo-python's ``construct_post_url``:
    scheme://host/path, then ``?`` + the re-sorted query (+ ``.`` when there is
    one) when the body has params, then each body ``{name}{value}`` sorted by name."""
    parts = urlsplit(url)
    signed = f"{parts.scheme}://{parts.netloc}{parts.path}"
    query = parse_qs(parts.query, keep_blank_values=True)
    sorted_query = "&".join(
        "&".join(f"{key}={value}" for value in sorted(query[key])) for key in sorted(query)
    )
    if not params:
        return signed + (f"?{sorted_query}" if sorted_query else "")
    signed += "?" + (f"{sorted_query}." if sorted_query else "")
    return signed + "".join(f"{key}{params[key]}" for key in sorted(params))


def compute_signature(url: str, params: dict[str, str], nonce: str, auth_token: str) -> str:
    message = f"{_signed_string(url, params)}.{nonce}".encode()
    digest = hmac.new(auth_token.encode(), message, hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def verify_signature(
    *, url: str, params: dict[str, str], auth_token: str,
    signature: str | None, nonce: str | None,
) -> None:
    """Raise unless ``signature`` (may list several, comma-separated) matches."""
    if not auth_token or not signature or not nonce:
        raise PlivoSignatureInvalidError()
    expected = compute_signature(url, params, nonce, auth_token)
    if not any(hmac.compare_digest(expected, s.strip()) for s in signature.split(",")):
        raise PlivoSignatureInvalidError()


async def mint_browser_token(
    *, auth_id: str, auth_token: str, endpoint_username: str, app_id: str,
    ttl_seconds: int = 300,
) -> str:
    """A short-lived outgoing-only JWT for the Browser SDK. Plivo rejects
    locally-signed tokens, so it must come from the REST API. Raises
    ``httpx.HTTPError`` on any failure."""
    now = int(time.time())
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(
            f"{PLIVO_API_BASE}/Account/{auth_id}/JWT/Token/",
            auth=(auth_id, auth_token),
            json={
                "iss": auth_id,
                "sub": endpoint_username,
                "nbf": now,
                "exp": now + ttl_seconds,
                "per": {"voice": {"incoming_allow": False, "outgoing_allow": True}},
                "app": app_id,
            },
        )
        response.raise_for_status()
    return str(response.json()["token"])


def xml(body: str) -> Response:
    return Response(
        content=f'<?xml version="1.0" encoding="UTF-8"?><Response>{body}</Response>',
        media_type="application/xml",
    )


def speak(text: str, voice: str) -> str:
    return f"<Speak voice={quoteattr(voice)}>{escape(text)}</Speak>"


def listen(prompt: str, action_url: str, voice: str) -> str:
    """Speak ``prompt`` and capture the caller's next utterance (posted to
    ``action_url`` as ``Speech``). No input -> the Redirect posts there empty."""
    return (
        f'<GetInput action={quoteattr(action_url)} method="POST" inputType="speech" '
        'language="en-US" speechModel="phone_call" executionTimeout="20" '
        'speechEndTimeout="auto" redirect="true">'
        f"{speak(prompt, voice)}</GetInput>"
        f'<Redirect method="POST">{escape(action_url)}</Redirect>'
    )

"""Signed, single-purpose unsubscribe tokens (A2 E4).

``token = base64url("{tenant_id}.{contact_id}.{email}") + "." + base64url(hmac)``.
The MAC covers a ``unsub.v1`` prefix, so no other signed value in the system
can be replayed as an unsubscribe token. No expiry: CAN-SPAM requires the link
to keep working for at least 30 days, and an unsubscribe is harmless to repeat.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
from dataclasses import dataclass

from common.errors import ValidationError

from api.config import get_api_settings

_PURPOSE = b"unsub.v1"


@dataclass(frozen=True)
class UnsubscribeTarget:
    tenant_id: str
    contact_id: str
    email: str


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def link_secret() -> bytes:
    secret = get_api_settings().email_link_secret
    if not secret or len(secret) < 32:
        raise ValidationError("EMAIL_LINK_SECRET (>= 32 chars) is not configured.",
                              code="EMAIL_NOT_READY")
    return secret.encode()


def _mac(payload: bytes, secret: bytes) -> bytes:
    return hmac.new(secret, _PURPOSE + b"." + payload, hashlib.sha256).digest()


def sign_unsubscribe(tenant_id: str, contact_id: str, email: str) -> str:
    payload = f"{tenant_id}.{contact_id}.{email.strip().lower()}".encode()
    return f"{_b64(payload)}.{_b64(_mac(payload, link_secret()))}"


def verify_unsubscribe(token: str) -> UnsubscribeTarget | None:
    """The token's target, or ``None`` for anything malformed or forged."""
    try:
        payload_b64, mac_b64 = token.split(".")
        payload, mac = _unb64(payload_b64), _unb64(mac_b64)
        tenant_id, contact_id, email = payload.decode().split(".", 2)
    except (ValueError, UnicodeDecodeError):
        return None
    if not hmac.compare_digest(mac, _mac(payload, link_secret())):
        return None
    return UnsubscribeTarget(tenant_id=tenant_id, contact_id=contact_id, email=email)


def unsubscribe_url(tenant_id: str, contact_id: str, email: str) -> str:
    base = get_api_settings().public_api_base_url
    if not base:
        raise ValidationError("PUBLIC_API_BASE_URL is not configured.", code="EMAIL_NOT_READY")
    token = sign_unsubscribe(tenant_id, contact_id, email)
    return f"{base.rstrip('/')}/public/email/unsubscribe/{token}"


# -- Inbound route tokens (A3 F1) ---------------------------------------------------
# A different purpose prefix, so an unsubscribe token can never pass as a route
# token (or the reverse). The URL is the credential: Inbound Parse can't sign.

_ROUTE_PURPOSE = b"inbound.v1"


def sign_route(tenant_id: str) -> str:
    payload = tenant_id.encode()
    mac = hmac.new(link_secret(), _ROUTE_PURPOSE + b"." + payload, hashlib.sha256).digest()
    return f"{_b64(payload)}.{_b64(mac)}"


def verify_route(token: str) -> str | None:
    """The tenant id the route token was issued for, or ``None``."""
    try:
        payload_b64, mac_b64 = token.split(".")
        payload, mac = _unb64(payload_b64), _unb64(mac_b64)
        tenant_id = payload.decode()
    except (ValueError, UnicodeDecodeError):
        return None
    expected = hmac.new(link_secret(), _ROUTE_PURPOSE + b"." + payload, hashlib.sha256).digest()
    return tenant_id if tenant_id and hmac.compare_digest(mac, expected) else None


def public_url(path: str) -> str:
    base = get_api_settings().public_api_base_url
    if not base:
        raise ValidationError("PUBLIC_API_BASE_URL is not configured.", code="EMAIL_NOT_READY")
    return f"{base.rstrip('/')}{path}"

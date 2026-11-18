"""The "manage your booking" link (plan 1.6): a signed, single-purpose token naming ONE
booking of ONE tenant. ``token = base64url("{tenant_id}.{event_id}") + "." +
base64url(hmac("booking.v1." + payload))`` -- the purpose prefix means no other signed value
in the system (an unsubscribe or inbound-route token) can pass as one. No expiry: the page
itself refuses bookings that are no longer upcoming.
"""
from __future__ import annotations

import base64
import hashlib
import hmac

from common.errors import ValidationError

from api.config import get_api_settings
from api.email_agent.links import link_secret

_PURPOSE = b"booking.v1"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _mac(payload: bytes) -> bytes:
    return hmac.new(link_secret(), _PURPOSE + b"." + payload, hashlib.sha256).digest()


def sign(tenant_id: str, event_id: str) -> str:
    payload = f"{tenant_id}.{event_id}".encode()
    return f"{_b64(payload)}.{_b64(_mac(payload))}"


def verify(token: str) -> tuple[str, str] | None:
    """``(tenant_id, event_id)``, or ``None`` for anything malformed or forged."""
    try:
        payload_b64, mac_b64 = token.split(".")
        payload, mac = _unb64(payload_b64), _unb64(mac_b64)
        tenant_id, event_id = payload.decode().split(".", 1)
    except (ValueError, UnicodeDecodeError):
        return None
    if not hmac.compare_digest(mac, _mac(payload)):
        return None
    return tenant_id, event_id


def manage_url(tenant_id: str, event_id: str) -> str | None:
    """The page's address, or ``None`` when the link secret or the public address isn't
    configured (the confirmation then keeps its old "contact us" line -- said, not hidden)."""
    base = get_api_settings().public_api_base_url
    if not base:
        return None
    try:
        return f"{base.rstrip('/')}/public/bookings/{sign(tenant_id, event_id)}"
    except ValidationError:  # EMAIL_LINK_SECRET missing
        return None

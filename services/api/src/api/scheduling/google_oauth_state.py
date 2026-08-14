"""Google OAuth ``state`` token store (SR-22) -- Redis-only, single-use via GETDEL.

Mirrors ``api.auth.password_reset``'s ``PasswordResetStore`` exactly: an
opaque, unguessable, single-use, TTL'd token, so this doesn't invent a
second pattern for the same shape of problem. ``issue`` binds the token to
the requesting tenant_id; ``consume`` is atomic (GETDEL) so a ``state`` value
can never be replayed against a second callback -- the standard CSRF defense
for an OAuth authorization-code flow.

The admin callback route ALSO re-checks the resulting tenant_id against the
caller's own authenticated session claims (defense in depth) -- this store
is the primary guard, not the only one.
"""
from __future__ import annotations

import hashlib
import secrets
from typing import Protocol

from common.errors import InternalServerError
from fastapi import Request

GOOGLE_OAUTH_STATE_PREFIX = "scheduling:google_oauth_state:"


def _hash_state(state: str) -> str:
    """SHA-256 hex digest of the raw state token."""
    return hashlib.sha256(state.encode()).hexdigest()


class GoogleOAuthStateStore(Protocol):
    async def issue(self, tenant_id: str, ttl_seconds: int) -> str: ...
    async def consume(self, state: str) -> str | None: ...


class RedisGoogleOAuthStateStore:
    """Redis-backed OAuth state store.

    Stores only SHA-256 hashes; consumes atomically via GETDEL.
    """

    def __init__(self, client: object) -> None:
        self._client = client

    async def issue(self, tenant_id: str, ttl_seconds: int) -> str:
        state = secrets.token_urlsafe(32)
        key = f"{GOOGLE_OAUTH_STATE_PREFIX}{_hash_state(state)}"
        await self._client.set(key, tenant_id, ex=max(1, ttl_seconds))  # type: ignore[attr-defined]
        return state

    async def consume(self, state: str) -> str | None:
        raw = await self._client.getdel(  # type: ignore[attr-defined]
            f"{GOOGLE_OAUTH_STATE_PREFIX}{_hash_state(state)}"
        )
        if raw is None:
            return None
        if isinstance(raw, bytes):
            return raw.decode()
        return str(raw)


def get_google_oauth_state_store(request: Request) -> GoogleOAuthStateStore:
    """Resolve the ``GoogleOAuthStateStore`` for this request.

    Raises ``InternalServerError`` if Redis is not configured (same posture
    as ``get_password_reset_store``/``get_token_blacklist``).
    """
    redis_client = getattr(request.app.state, "redis", None)
    if redis_client is None:
        raise InternalServerError("Google OAuth state requires Redis.")
    return RedisGoogleOAuthStateStore(redis_client)

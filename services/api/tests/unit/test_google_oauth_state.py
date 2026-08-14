"""Unit tests for api.scheduling.google_oauth_state (SR-22).

Covers the RedisGoogleOAuthStateStore's issue/consume roundtrip, single-use
(GETDEL) semantics, that only a SHA-256 hash of the raw state is ever stored
(never the raw value itself), and get_google_oauth_state_store's Redis-
required posture -- mirrors test_password_reset.py's own Redis-double style.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from common.errors import InternalServerError

from api.scheduling.google_oauth_state import (
    GOOGLE_OAUTH_STATE_PREFIX,
    RedisGoogleOAuthStateStore,
    get_google_oauth_state_store,
)


class _RecordingRedis:
    """Minimal Redis double: only set/getdel, matching what this store uses."""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}
        self.set_calls: list[tuple[str, str, int | None]] = []
        self.getdel_calls: list[str] = []

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.set_calls.append((key, value, ex))
        self._store[key] = value

    async def getdel(self, key: str) -> str | None:
        self.getdel_calls.append(key)
        return self._store.pop(key, None)


# ==============================================================================
# RedisGoogleOAuthStateStore
# ==============================================================================


async def test_issue_then_consume_roundtrip_returns_tenant_id() -> None:
    redis = _RecordingRedis()
    store = RedisGoogleOAuthStateStore(redis)

    state = await store.issue("tenant-a", 600)
    consumed = await store.consume(state)

    assert consumed == "tenant-a"


async def test_issue_stores_only_a_hash_never_the_raw_state() -> None:
    """The raw, unguessable state token must never be readable back out of
    Redis storage -- only its SHA-256 hash is the key, and the tenant_id is
    the only stored value."""
    redis = _RecordingRedis()
    store = RedisGoogleOAuthStateStore(redis)

    state = await store.issue("tenant-a", 600)

    assert len(redis.set_calls) == 1
    key, value, ex = redis.set_calls[0]
    assert key.startswith(GOOGLE_OAUTH_STATE_PREFIX)
    assert state not in key
    assert value == "tenant-a"
    assert ex == 600


async def test_consume_unknown_state_returns_none() -> None:
    redis = _RecordingRedis()
    store = RedisGoogleOAuthStateStore(redis)

    consumed = await store.consume("never-issued")

    assert consumed is None


async def test_consume_is_single_use_second_call_returns_none() -> None:
    """GETDEL semantics: a state value can never be replayed against a
    second callback -- the standard CSRF defense for this flow."""
    redis = _RecordingRedis()
    store = RedisGoogleOAuthStateStore(redis)

    state = await store.issue("tenant-a", 600)

    first = await store.consume(state)
    second = await store.consume(state)

    assert first == "tenant-a"
    assert second is None


async def test_issue_ttl_floor_is_at_least_one_second() -> None:
    redis = _RecordingRedis()
    store = RedisGoogleOAuthStateStore(redis)

    await store.issue("tenant-a", 0)

    _, _, ex = redis.set_calls[0]
    assert ex == 1


# ==============================================================================
# get_google_oauth_state_store
# ==============================================================================


def test_get_store_raises_when_redis_not_configured() -> None:
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(redis=None)))

    with pytest.raises(InternalServerError):
        get_google_oauth_state_store(request)  # type: ignore[arg-type]


def test_get_store_returns_redis_backed_store_when_configured() -> None:
    redis = _RecordingRedis()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(redis=redis)))

    store = get_google_oauth_state_store(request)  # type: ignore[arg-type]

    assert isinstance(store, RedisGoogleOAuthStateStore)

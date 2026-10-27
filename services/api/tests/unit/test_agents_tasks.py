"""The agents worker's DB connection must speak pgvector.

The email composer and reply answering search the knowledge base from inside these
Celery tasks; without the vector codec every first e-mail failed with
``DataError: invalid input for query argument $1 ... (expected str, got list)``.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from common.pgvector import register_vector_init

from api.agents import tasks


def test_worker_connection_registers_the_vector_codec() -> None:
    seen: dict[str, Any] = {}

    async def fake_connect(dsn: str, **kwargs: Any) -> Any:
        seen.update(kwargs, dsn=dsn)
        return SimpleNamespace(close=AsyncMock())

    async def body(_db: Any, _settings: Any) -> str:
        return "ran"

    with patch("api.config.get_api_settings",
               return_value=SimpleNamespace(database_url="postgresql://stub/db")), \
            patch.object(tasks.Database, "connect", side_effect=fake_connect):
        assert tasks._run(body) == "ran"

    assert seen["init"] is register_vector_init
    assert seen["statement_cache_size"] == 0  # still PgBouncer-safe

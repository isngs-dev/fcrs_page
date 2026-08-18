"""Unit tests for debug LLM routes (POST /debug/llm/config, POST /debug/llm/generate,
POST /debug/llm/embed, POST /debug/llm/classify, POST /debug/llm/stream).

Covers:
- POST /debug/llm/config: CLIENT_ADMIN → 200 (no api_key in response); ciphertext stored;
  CLIENT_AGENT → 403; no cookie → 401.
- POST /debug/llm/generate: with stub config + stub provider → 200 {text,...};
  no config → 422 LLM_NOT_CONFIGURED; CLIENT_AGENT → 403.
- POST /debug/llm/embed: with stub config + stub provider → 200 {model, count, dimension};
  no config → 422; CLIENT_AGENT → 403; no cookie → 401.
- POST /debug/llm/classify: with stub config + stub provider → 200 {label, model};
  no config → 422; CLIENT_AGENT → 403; no cookie → 401.
- POST /debug/llm/stream: with stub config + stub provider → 200 streaming text;
  CLIENT_AGENT → 403; no cookie → 401.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import patch

from common.auth import AuthClaims, Role
from common.cache import InMemoryCache
from common.crypto import SecretBox
from httpx import ASGITransport, AsyncClient

from api.auth.tokens import create_access_token
from api.config import get_api_settings
from api.llm.provider import ChatMessage, Chunk, Completion, Label, Vector

# -- Constants -----------------------------------------------------------------

_TEST_JWT_SECRET = "x" * 48
_TENANT_ID = "tenant-abc-123"
_TEST_ENCRYPTION_KEY = "x" * 48

# -- Test doubles --------------------------------------------------------------


class _StubDatabase:
    """Database double that can return a config row or None."""

    def __init__(self, *, config_row: dict[str, Any] | None = None) -> None:
        self._config_row = config_row
        self.last_sql: str = ""
        self.last_params: tuple[Any, ...] = ()

    async def fetchrow(self, query: str, *args: object) -> dict[str, Any] | None:
        self.last_sql = query
        self.last_params = args
        return self._config_row

    async def execute(self, query: str, *args: object) -> str:
        self.last_sql = query
        self.last_params = args
        return "INSERT 1"

    async def close(self) -> None:
        pass


class _StubRedis:
    async def get(self, key: str) -> str | None:
        return None

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        pass

    async def getdel(self, key: str) -> str | None:
        return None

    async def ping(self) -> bool:
        return True

    async def aclose(self) -> None:
        pass

    def pipeline(self, transaction: bool = False) -> _StubPipeline:
        return _StubPipeline()


class _StubPipeline:
    def zremrangebyscore(self, key: str, min_score: float, max_score: float) -> None:
        pass

    def zadd(self, key: str, mapping: dict[str, float]) -> None:
        pass

    def zcard(self, key: str) -> None:
        pass

    def expire(self, key: str, seconds: int) -> None:
        pass

    async def execute(self) -> list[Any]:
        return [0, None, 0, True]

    async def zrange(self, key: str, start: int, end: int, withscores: bool = False) -> list:
        return []


class _StubProvider:
    """Stub LLM provider that returns canned Completion, vectors, label, and stream chunks."""

    def __init__(
        self,
        text: str = "Stub response.",
        vectors: list[Vector] | None = None,
        label: Label = "Support",
        stream_chunks: list[Chunk] | None = None,
    ) -> None:
        self._text = text
        self._vectors = vectors or [[0.1, 0.2, 0.3]]
        self._label = label
        self._stream_chunks = stream_chunks or [Chunk("He"), Chunk("llo")]
        self.aclose_calls = 0

    async def aclose(self) -> None:
        self.aclose_calls += 1

    async def generate(
        self,
        messages: list[ChatMessage],
        *,
        model: str,
        max_tokens: int,
    ) -> Completion:
        return Completion(
            text=self._text,
            model=model,
            input_tokens=10,
            output_tokens=5,
        )

    async def embed(
        self,
        texts: list[str],
        *,
        model: str,
    ) -> list[Vector]:
        return self._vectors

    async def classify(
        self,
        text: str,
        labels: list[str],
        *,
        model: str,
        label_descriptions: dict[str, str] | None = None,
    ) -> Label:
        return self._label

    async def stream(
        self,
        messages: list[ChatMessage],
        *,
        model: str,
        max_tokens: int,
    ):
        for chunk in self._stream_chunks:
            yield chunk


# -- Helpers -------------------------------------------------------------------

_TEST_SETTINGS_ENV = {
    "DEPLOYMENT_MODE": "saas",
    "DATABASE_URL": "postgres://stub-host:5432/appdb",
    "REDIS_URL": "redis://stub-host:6379",
    "JWT_SECRET": _TEST_JWT_SECRET,
    "SECRET_ENCRYPTION_KEY": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    "SERVICE_NAME": "api",
    "LOG_LEVEL": "WARNING",
    "COOKIE_SECURE": "false",
}


def _build_app(db: Any = None) -> Any:
    """Create app with test doubles."""
    from common.settings import get_settings

    from api.config import get_api_settings

    get_settings.cache_clear()
    get_api_settings.cache_clear()

    with patch.dict("os.environ", _TEST_SETTINGS_ENV, clear=False):
        from api.app import create_app

        app = create_app()

    app.state.db = db if db is not None else _StubDatabase()
    app.state.redis = _StubRedis()
    app.state.cache = InMemoryCache()
    app.state.rate_limiter = None
    return app


def _mint_cookie(
    *,
    subject: str = "user-1",
    role: Role = Role.CLIENT_ADMIN,
    tenant_id: str | None = _TENANT_ID,
    ttl_seconds: int = 300,
    secret: str = _TEST_JWT_SECRET,
) -> str:
    claims = AuthClaims(subject=subject, role=role, tenant_id=tenant_id)
    token, _ = create_access_token(claims, secret=secret, ttl_seconds=ttl_seconds)
    return token


# ==============================================================================
# POST /debug/llm/config
# ==============================================================================


async def test_llm_config_client_admin_returns_200() -> None:
    """CLIENT_ADMIN → 200, response has provider+model, no api_key."""
    db = _StubDatabase()
    app = _build_app(db=db)
    token = _mint_cookie(role=Role.CLIENT_ADMIN)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/debug/llm/config",
            json={"provider": "anthropic", "model": "claude-opus-4-8", "api_key": "sk-test-key"},
            cookies={"access_token": token},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["provider"] == "anthropic"
    assert body["model"] == "claude-opus-4-8"
    assert "api_key" not in body

    # Verify ciphertext stored (not plaintext)
    ciphertext = db.last_params[3]
    assert isinstance(ciphertext, str)
    assert ciphertext != "sk-test-key"
    box = SecretBox(get_api_settings().secret_encryption_key)
    assert box.decrypt_str(ciphertext) == "sk-test-key"


async def test_llm_config_openai_with_base_url_returns_200() -> None:
    """CLIENT_ADMIN with provider=openai + base_url → 200, base_url stored."""
    db = _StubDatabase()
    app = _build_app(db=db)
    token = _mint_cookie(role=Role.CLIENT_ADMIN)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/debug/llm/config",
            json={
                "provider": "openai",
                "base_url": "https://opencode.ai/zen/v1",
                "model": "gpt-4o",
                "api_key": "sk-zen-key",
            },
            cookies={"access_token": token},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["provider"] == "openai"
    assert body["model"] == "gpt-4o"
    assert "api_key" not in body

    # Verify base_url stored
    assert db.last_params[4] == "https://opencode.ai/zen/v1"
    # Verify ciphertext stored (not plaintext)
    ciphertext = db.last_params[3]
    assert ciphertext != "sk-zen-key"
    # Verify api_version stored (6th param)
    assert db.last_params[5] is None


async def test_llm_config_azure_with_api_version_returns_200() -> None:
    """CLIENT_ADMIN with provider=azure + api_version → 200, api_version stored."""
    db = _StubDatabase()
    app = _build_app(db=db)
    token = _mint_cookie(role=Role.CLIENT_ADMIN)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/debug/llm/config",
            json={
                "provider": "azure",
                "base_url": "https://my-resource.openai.azure.com",
                "api_version": "2024-02-01",
                "model": "my-deployment",
                "api_key": "sk-azure-key",
            },
            cookies={"access_token": token},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["provider"] == "azure"
    assert body["model"] == "my-deployment"
    assert "api_key" not in body

    # Verify api_version stored (6th param)
    assert db.last_params[5] == "2024-02-01"
    # Verify base_url stored (5th param)
    assert db.last_params[4] == "https://my-resource.openai.azure.com"


async def test_llm_config_client_agent_returns_403() -> None:
    """CLIENT_AGENT → 403."""
    app = _build_app()
    token = _mint_cookie(role=Role.CLIENT_AGENT)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/debug/llm/config",
            json={"provider": "anthropic", "model": "claude-opus-4-8", "api_key": "sk-test-key"},
            cookies={"access_token": token},
        )
    assert resp.status_code == 403


async def test_llm_config_no_cookie_returns_401() -> None:
    """No cookie → 401."""
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/debug/llm/config",
            json={"provider": "anthropic", "model": "claude-opus-4-8", "api_key": "sk-test-key"},
        )
    assert resp.status_code == 401


# ==============================================================================
# POST /debug/llm/generate
# ==============================================================================


async def test_llm_generate_with_config_returns_200() -> None:
    """With a stub config + stub provider → 200 {text,...}."""
    ciphertext = SecretBox(get_api_settings().secret_encryption_key).encrypt("sk-test-key")
    config_row = {
        "provider": "anthropic",
        "model": "claude-opus-4-8",
        "api_key_ciphertext": ciphertext,
        "base_url": None,
        "api_version": None,
    }
    db = _StubDatabase(config_row=config_row)
    app = _build_app(db=db)

    # Patch provider_for to return a stub
    provider = _StubProvider("Hello from Claude.")
    with patch("api.llm.routes.provider_for", return_value=provider):
        token = _mint_cookie(role=Role.CLIENT_ADMIN)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                "/debug/llm/generate",
                json={"prompt": "Hello"},
                cookies={"access_token": token},
            )
    assert resp.status_code == 200
    body = resp.json()
    assert body["text"] == "Hello from Claude."
    assert body["model"] == "claude-opus-4-8"
    assert body["input_tokens"] == 10
    assert body["output_tokens"] == 5
    # Resource-leak fix: the provider must be closed after generate.
    assert provider.aclose_calls == 1


async def test_llm_generate_no_config_returns_422() -> None:
    """Tenant with no config → 422 LLM_NOT_CONFIGURED."""
    db = _StubDatabase(config_row=None)
    app = _build_app(db=db)
    token = _mint_cookie(role=Role.CLIENT_ADMIN)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/debug/llm/generate",
            json={"prompt": "Hello"},
            cookies={"access_token": token},
        )
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "LLM_NOT_CONFIGURED"


async def test_llm_generate_client_agent_returns_403() -> None:
    """CLIENT_AGENT → 403."""
    app = _build_app()
    token = _mint_cookie(role=Role.CLIENT_AGENT)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/debug/llm/generate",
            json={"prompt": "Hello"},
            cookies={"access_token": token},
        )
    assert resp.status_code == 403


async def test_llm_generate_openai_with_base_url_returns_200() -> None:
    """OpenAI config with base_url + stub provider → 200 {text,...}."""
    ciphertext = SecretBox(get_api_settings().secret_encryption_key).encrypt("sk-zen-key")
    config_row = {
        "provider": "openai",
        "model": "gpt-4o",
        "api_key_ciphertext": ciphertext,
        "base_url": "https://opencode.ai/zen/v1",
        "api_version": None,
    }
    db = _StubDatabase(config_row=config_row)
    app = _build_app(db=db)

    with patch("api.llm.routes.provider_for", return_value=_StubProvider("Hello from Zen.")):
        token = _mint_cookie(role=Role.CLIENT_ADMIN)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                "/debug/llm/generate",
                json={"prompt": "Say hello"},
                cookies={"access_token": token},
            )
    assert resp.status_code == 200
    body = resp.json()
    assert body["text"] == "Hello from Zen."
    assert body["model"] == "gpt-4o"


# ==============================================================================
# POST /debug/llm/embed
# ==============================================================================


async def test_llm_embed_with_config_returns_200() -> None:
    """With a stub config + stub provider → 200 {model, count, dimension}."""
    ciphertext = SecretBox(get_api_settings().secret_encryption_key).encrypt("sk-test-key")
    config_row = {
        "provider": "openai",
        "model": "text-embedding-3-small",
        "api_key_ciphertext": ciphertext,
        "base_url": None,
        "api_version": None,
    }
    db = _StubDatabase(config_row=config_row)
    app = _build_app(db=db)

    stub_vectors = [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]
    provider = _StubProvider(vectors=stub_vectors)
    with patch(
        "api.llm.routes.provider_for",
        return_value=provider,
    ):
        token = _mint_cookie(role=Role.CLIENT_ADMIN)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                "/debug/llm/embed",
                json={"texts": ["hello", "world"], "model": "text-embedding-3-small"},
                cookies={"access_token": token},
            )
    assert resp.status_code == 200
    body = resp.json()
    assert body["model"] == "text-embedding-3-small"
    assert body["count"] == 2
    assert body["dimension"] == 3
    # Resource-leak fix: the provider must be closed after embed.
    assert provider.aclose_calls == 1


async def test_llm_embed_no_config_returns_422() -> None:
    """Tenant with no config → 422 LLM_NOT_CONFIGURED."""
    db = _StubDatabase(config_row=None)
    app = _build_app(db=db)
    token = _mint_cookie(role=Role.CLIENT_ADMIN)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/debug/llm/embed",
            json={"texts": ["hello"], "model": "nomic-embed-text"},
            cookies={"access_token": token},
        )
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "LLM_NOT_CONFIGURED"


async def test_llm_embed_client_agent_returns_403() -> None:
    """CLIENT_AGENT → 403."""
    app = _build_app()
    token = _mint_cookie(role=Role.CLIENT_AGENT)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/debug/llm/embed",
            json={"texts": ["hello"], "model": "nomic-embed-text"},
            cookies={"access_token": token},
        )
    assert resp.status_code == 403


async def test_llm_embed_no_cookie_returns_401() -> None:
    """No cookie → 401."""
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/debug/llm/embed",
            json={"texts": ["hello"], "model": "nomic-embed-text"},
        )
    assert resp.status_code == 401


# ==============================================================================
# POST /debug/llm/classify
# ==============================================================================


async def test_llm_classify_with_config_returns_200() -> None:
    """With a stub config + stub provider → 200 {label, model}."""
    ciphertext = SecretBox(get_api_settings().secret_encryption_key).encrypt("sk-test-key")
    config_row = {
        "provider": "openai",
        "model": "gpt-4o",
        "api_key_ciphertext": ciphertext,
        "base_url": None,
        "api_version": None,
    }
    db = _StubDatabase(config_row=config_row)
    app = _build_app(db=db)

    provider = _StubProvider(label="Support")
    with patch(
        "api.llm.routes.provider_for",
        return_value=provider,
    ):
        token = _mint_cookie(role=Role.CLIENT_ADMIN)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                "/debug/llm/classify",
                json={"text": "I need help", "labels": ["Sales", "Support", "Billing"]},
                cookies={"access_token": token},
            )
    assert resp.status_code == 200
    body = resp.json()
    assert body["label"] == "Support"
    assert body["model"] == "gpt-4o"
    # Resource-leak fix: the provider must be closed after classify.
    assert provider.aclose_calls == 1


async def test_llm_classify_no_config_returns_422() -> None:
    """Tenant with no config → 422 LLM_NOT_CONFIGURED."""
    db = _StubDatabase(config_row=None)
    app = _build_app(db=db)
    token = _mint_cookie(role=Role.CLIENT_ADMIN)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/debug/llm/classify",
            json={"text": "I need help", "labels": ["Sales", "Support"]},
            cookies={"access_token": token},
        )
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "LLM_NOT_CONFIGURED"


async def test_llm_classify_client_agent_returns_403() -> None:
    """CLIENT_AGENT → 403."""
    app = _build_app()
    token = _mint_cookie(role=Role.CLIENT_AGENT)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/debug/llm/classify",
            json={"text": "I need help", "labels": ["Sales", "Support"]},
            cookies={"access_token": token},
        )
    assert resp.status_code == 403


async def test_llm_classify_no_cookie_returns_401() -> None:
    """No cookie → 401."""
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/debug/llm/classify",
            json={"text": "I need help", "labels": ["Sales", "Support"]},
        )
    assert resp.status_code == 401


# ==============================================================================
# POST /debug/llm/stream
# ==============================================================================


async def test_llm_stream_with_config_returns_200() -> None:
    """With a stub config + stub provider → 200, body equals joined deltas."""
    ciphertext = SecretBox(get_api_settings().secret_encryption_key).encrypt("sk-test-key")
    config_row = {
        "provider": "openai",
        "model": "gpt-4o",
        "api_key_ciphertext": ciphertext,
        "base_url": None,
        "api_version": None,
    }
    db = _StubDatabase(config_row=config_row)
    app = _build_app(db=db)

    stream_chunks = [Chunk("Hello"), Chunk(" from the bot.")]
    provider = _StubProvider(stream_chunks=stream_chunks)
    with patch(
        "api.llm.routes.provider_for",
        return_value=provider,
    ):
        token = _mint_cookie(role=Role.CLIENT_ADMIN)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                "/debug/llm/stream",
                json={"prompt": "Say hello"},
                cookies={"access_token": token},
            )
    assert resp.status_code == 200
    assert resp.text == "Hello from the bot."
    # Resource-leak fix: the provider must be closed only AFTER the stream
    # body has been fully consumed by the client above.
    assert provider.aclose_calls == 1


async def test_llm_stream_client_agent_returns_403() -> None:
    """CLIENT_AGENT → 403."""
    app = _build_app()
    token = _mint_cookie(role=Role.CLIENT_AGENT)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/debug/llm/stream",
            json={"prompt": "Say hello"},
            cookies={"access_token": token},
        )
    assert resp.status_code == 403


async def test_llm_stream_no_cookie_returns_401() -> None:
    """No cookie → 401."""
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/debug/llm/stream",
            json={"prompt": "Say hello"},
        )
    assert resp.status_code == 401


# ==============================================================================
# S5.3: POST /debug/llm/config — embedding_model stored and echoed, api_key not
# ==============================================================================


async def test_llm_config_stores_and_echoes_embedding_model() -> None:
    """embedding_model is stored (7th param) and echoed in the response."""
    db = _StubDatabase()
    app = _build_app(db=db)
    token = _mint_cookie(role=Role.CLIENT_ADMIN)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/debug/llm/config",
            json={
                "provider": "openai",
                "base_url": "http://localhost:11434/v1",
                "model": "qwen:0.5b",
                "api_key": "ollama",
                "embedding_model": "nomic-embed-text",
            },
            cookies={"access_token": token},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["embedding_model"] == "nomic-embed-text"
    assert "api_key" not in body

    # 7th SQL param must be embedding_model.
    assert db.last_params[6] == "nomic-embed-text"


async def test_llm_config_without_embedding_model_no_field_in_response() -> None:
    """When embedding_model is omitted, the response does not echo it (None → absent)."""
    db = _StubDatabase()
    app = _build_app(db=db)
    token = _mint_cookie(role=Role.CLIENT_ADMIN)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/debug/llm/config",
            json={"provider": "anthropic", "model": "claude-opus-4-8", "api_key": "sk-key"},
            cookies={"access_token": token},
        )
    assert resp.status_code == 200
    body = resp.json()
    # api_key must never appear.
    assert "api_key" not in body
    # embedding_model not sent → not in response.
    assert "embedding_model" not in body


async def test_llm_config_api_key_never_echoed_even_with_embedding_model() -> None:
    """api_key is never present in the response body regardless of other fields."""
    db = _StubDatabase()
    app = _build_app(db=db)
    token = _mint_cookie(role=Role.CLIENT_ADMIN)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/debug/llm/config",
            json={
                "provider": "openai",
                "model": "text-embedding-3-small",
                "api_key": "sk-very-secret",
                "embedding_model": "text-embedding-3-small",
            },
            cookies={"access_token": token},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert "api_key" not in body
    assert "sk-very-secret" not in str(body)


# ==============================================================================
# SR-24: POST /debug/llm/config — embedding_base_url stored and echoed
# ==============================================================================


async def test_llm_config_stores_and_echoes_embedding_base_url() -> None:
    """embedding_base_url is stored (8th param) and echoed in the response."""
    db = _StubDatabase()
    app = _build_app(db=db)
    token = _mint_cookie(role=Role.CLIENT_ADMIN)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/debug/llm/config",
            json={
                "provider": "openai",
                "base_url": "https://ollama.com/v1",
                "model": "gpt-oss:20b",
                "api_key": "ollama",
                "embedding_model": "all-MiniLM-L6-v2",
                "embedding_base_url": "http://embeddings:8080/v1",
            },
            cookies={"access_token": token},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["embedding_base_url"] == "http://embeddings:8080/v1"
    assert "api_key" not in body

    # 8th SQL param must be embedding_base_url.
    assert db.last_params[7] == "http://embeddings:8080/v1"


async def test_llm_config_without_embedding_base_url_no_field_in_response() -> None:
    """When embedding_base_url is omitted, the response does not echo it."""
    db = _StubDatabase()
    app = _build_app(db=db)
    token = _mint_cookie(role=Role.CLIENT_ADMIN)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/debug/llm/config",
            json={"provider": "anthropic", "model": "claude-opus-4-8", "api_key": "sk-key"},
            cookies={"access_token": token},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert "embedding_base_url" not in body


# ==============================================================================
# SR-26: POST /debug/llm/config — embedding_api_key never echoed, embedding_dimensions echoed
# ==============================================================================


async def test_llm_config_embedding_api_key_never_echoed_even_when_set() -> None:
    """embedding_api_key is stored (9th SQL param) but NEVER appears in the
    response -- same never-echo rule as the chat api_key."""
    db = _StubDatabase()
    app = _build_app(db=db)
    token = _mint_cookie(role=Role.CLIENT_ADMIN)
    embedding_key_value = "openai-embedding"
    embedding_key = "sk-" + embedding_key_value + "-secret-key"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/debug/llm/config",
            json={
                "provider": "openai",
                "base_url": "https://ollama.com/v1",
                "model": "gpt-oss:20b",
                "api_key": "ollama",
                "embedding_model": "text-embedding-3-small",
                "embedding_base_url": "https://api.openai.com/v1",
                "embedding_api_key": embedding_key,
            },
            cookies={"access_token": token},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert "embedding_api_key" not in body
    assert "api_key" not in body
    assert embedding_key not in str(body)

    # 9th SQL param must be embedding_api_key's ciphertext -- never the plaintext.
    assert db.last_params[8] != embedding_key
    assert isinstance(db.last_params[8], str)


async def test_llm_config_stores_and_echoes_embedding_dimensions() -> None:
    """embedding_dimensions is stored (10th param) and echoed in the response."""
    db = _StubDatabase()
    app = _build_app(db=db)
    token = _mint_cookie(role=Role.CLIENT_ADMIN)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/debug/llm/config",
            json={
                "provider": "openai",
                "base_url": "https://ollama.com/v1",
                "model": "gpt-oss:20b",
                "api_key": "ollama",
                "embedding_model": "text-embedding-3-small",
                "embedding_dimensions": 768,
            },
            cookies={"access_token": token},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["embedding_dimensions"] == 768
    assert db.last_params[9] == 768


async def test_llm_config_without_embedding_dimensions_no_field_in_response() -> None:
    """When embedding_dimensions is omitted, the response does not echo it."""
    db = _StubDatabase()
    app = _build_app(db=db)
    token = _mint_cookie(role=Role.CLIENT_ADMIN)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/debug/llm/config",
            json={"provider": "anthropic", "model": "claude-opus-4-8", "api_key": "sk-key"},
            cookies={"access_token": token},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert "embedding_dimensions" not in body

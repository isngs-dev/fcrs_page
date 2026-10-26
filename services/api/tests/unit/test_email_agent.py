"""Email Agent pure pieces (A2): unsubscribe tokens, headers for both providers,
SendGrid status mapping, the commitment check, output parsing, composer bands."""
from __future__ import annotations

import json
from collections.abc import Iterator
from email import message_from_bytes
from typing import Any

import httpx
import pytest
from common.auth import AuthClaims, Role
from common.errors import ValidationError

from api.agents.executors import TransientExecutorError
from api.config import get_api_settings
from api.email_agent import composer
from api.email_agent.links import sign_unsubscribe, unsubscribe_url, verify_unsubscribe
from api.email_agent.providers import (
    EmailDeliveryError,
    OutboundEmail,
    SendGridEmailProvider,
    sendgrid_payload,
    smtp_message,
)
from api.orchestrator.guardrails import unstated_commitments

_SECRET = "s" * 40


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("EMAIL_LINK_SECRET", _SECRET)
    monkeypatch.setenv("PUBLIC_API_BASE_URL", "https://api.example.com/")
    get_api_settings.cache_clear()
    yield
    get_api_settings.cache_clear()


# -- Unsubscribe tokens --------------------------------------------------------------


def test_token_round_trip_and_url() -> None:
    token = sign_unsubscribe("tenant-a", "contact-1", " Jane@Example.com ")
    target = verify_unsubscribe(token)
    assert target is not None
    assert (target.tenant_id, target.contact_id, target.email) == (
        "tenant-a", "contact-1", "jane@example.com")
    assert unsubscribe_url("tenant-a", "contact-1", "jane@example.com").startswith(
        "https://api.example.com/public/email/unsubscribe/")


def test_tampered_or_retargeted_token_is_rejected() -> None:
    token = sign_unsubscribe("tenant-a", "contact-1", "jane@example.com")
    forged = sign_unsubscribe("tenant-b", "contact-1", "jane@example.com")
    # payload of tenant-b with tenant-a's MAC
    assert verify_unsubscribe(forged.split(".")[0] + "." + token.split(".")[1]) is None
    assert verify_unsubscribe(token[:-2] + "AA") is None
    for junk in ("", "abc", "a.b.c", "!!!.???"):
        assert verify_unsubscribe(junk) is None


def test_token_from_another_secret_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    token = sign_unsubscribe("tenant-a", "contact-1", "jane@example.com")
    monkeypatch.setenv("EMAIL_LINK_SECRET", "x" * 40)
    get_api_settings.cache_clear()
    assert verify_unsubscribe(token) is None


def test_missing_or_short_secret_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMAIL_LINK_SECRET", "short")
    get_api_settings.cache_clear()
    with pytest.raises(ValidationError) as exc:
        sign_unsubscribe("t", "c", "e@x.com")
    assert exc.value.code == "EMAIL_NOT_READY"


# -- Headers -----------------------------------------------------------------------------

_EMAIL = OutboundEmail(
    from_address="rebecca@mail.acme.com", from_name="Acme\r\nBcc: evil@x.com",
    to="jane@example.com", subject="Hello\nthere", body_text="Hi Jane,\n\nSee https://acme.com",
    message_id="<abc@mail.acme.com>", reply_to="sales@acme.com",
    in_reply_to="<first@mail.acme.com>", references=["<first@mail.acme.com>"],
    unsubscribe_url="https://api.example.com/public/email/unsubscribe/tok",
    custom_args={"message_id": "m1"},
)


def test_sendgrid_payload_headers() -> None:
    payload = sendgrid_payload(_EMAIL)
    headers = payload["headers"]
    assert headers["Message-ID"] == "<abc@mail.acme.com>"
    assert headers["In-Reply-To"] == "<first@mail.acme.com>"
    assert headers["References"] == "<first@mail.acme.com>"
    assert headers["List-Unsubscribe"] == "<https://api.example.com/public/email/unsubscribe/tok>"
    assert headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert payload["reply_to"] == {"email": "sales@acme.com"}
    assert payload["subject"] == "Hello there"           # no header injection
    assert "\n" not in payload["from"]["name"]
    assert [c["type"] for c in payload["content"]] == ["text/plain", "text/html"]
    assert payload["tracking_settings"]["click_tracking"]["enable"] is False


def test_smtp_message_headers() -> None:
    msg = message_from_bytes(smtp_message(_EMAIL).as_bytes())
    assert msg["Message-ID"] == "<abc@mail.acme.com>"
    assert msg["In-Reply-To"] == "<first@mail.acme.com>"
    assert msg["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert msg["Reply-To"] == "sales@acme.com"
    assert msg["Bcc"] is None


# -- SendGrid status mapping -------------------------------------------------------------


def _provider(status: int, headers: dict[str, str] | None = None) -> tuple[
        SendGridEmailProvider, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, headers=headers or {})

    return SendGridEmailProvider("SG.key", timeout=5,
                                 transport=httpx.MockTransport(handler)), seen


async def test_sendgrid_success_captures_provider_id() -> None:
    provider, seen = _provider(202, {"X-Message-Id": "sg-123"})
    ref = await provider.send(_EMAIL)
    assert ref.provider_message_id == "sg-123"
    assert seen[0].headers["Authorization"] == "Bearer SG.key"
    assert json.loads(seen[0].content)["personalizations"][0]["to"][0]["email"] == (
        "jane@example.com")


@pytest.mark.parametrize("status", [429, 500, 503])
async def test_sendgrid_transient_statuses(status: int) -> None:
    provider, _ = _provider(status)
    with pytest.raises(TransientExecutorError):
        await provider.send(_EMAIL)


@pytest.mark.parametrize("status", [400, 401, 403])
async def test_sendgrid_deterministic_statuses(status: int) -> None:
    provider, _ = _provider(status)
    with pytest.raises(EmailDeliveryError) as exc:
        await provider.send(_EMAIL)
    assert exc.value.code == f"SENDGRID_{status}"


async def test_sendgrid_network_error_is_transient() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow")

    provider = SendGridEmailProvider("k", timeout=1, transport=httpx.MockTransport(boom))
    with pytest.raises(TransientExecutorError):
        await provider.send(_EMAIL)


# -- Commitment check + parsing ----------------------------------------------------------


@pytest.mark.parametrize(("text", "context", "unstated"), [
    ("Our plan is $49 a month.", "Plans start at $49 per month.", []),
    ("Our plan is $39 a month.", "Plans start at $49 per month.", ["$39"]),
    ("Get 20% off this week.", "No discounts listed.", ["20%"]),
    ("Inspection is free.", "We offer a free roof inspection.", []),
    ("Inspection is free and guaranteed.", "We offer a free roof inspection.", ["guaranteed"]),
    ("We can help with your roof.", "", []),
])
def test_unstated_commitments(text: str, context: str, unstated: list[str]) -> None:
    assert unstated_commitments(text, context) == unstated


def test_parse_output() -> None:
    assert composer.parse_output("SUBJECT: Quick follow-up\n\nHi Jane,\nThanks.") == (
        "Quick follow-up", "Hi Jane,\nThanks.")
    assert composer.parse_output("Hi Jane, no subject here") is None


# -- Composer bands (LLM + retrieval faked) ----------------------------------------------------


class _Chunk:
    def __init__(self, content: str) -> None:
        self.chunk_id, self.doc_id, self.content = "c1", "d1", content


class _Provider:
    def __init__(self, text: str) -> None:
        self.text = text
        self.prompts: list[Any] = []

    async def generate(self, messages: list[Any], **_: Any) -> Any:
        self.prompts.append(messages)
        return type("C", (), {"text": self.text})()

    async def aclose(self) -> None:
        return None


_CLAIMS = AuthClaims(subject="system:agents", role=Role.CLIENT_ADMIN, tenant_id="t-1")


async def _compose(monkeypatch: pytest.MonkeyPatch, text: str, chunks: list[str]) -> Any:
    provider = _Provider(text)

    async def llm_config(db: Any, claims: Any) -> Any:
        return type("L", (), {"model": "m", "embedding_model": "e"})()

    async def retrieve(db: Any, claims: Any, query: str, *, k: int) -> Any:
        return type("R", (), {"chunks": [_Chunk(c) for c in chunks]})()

    async def no_summary(db: Any, claims: Any, visitor_ids: list[str]) -> None:
        return None

    monkeypatch.setattr(composer, "get_llm_config", llm_config)
    monkeypatch.setattr(composer, "retrieve_hybrid", retrieve)
    monkeypatch.setattr(composer, "provider_for", lambda config: provider)
    monkeypatch.setattr(composer, "chat_summary", no_summary)  # A11: moved to agents.memory
    result = await composer.compose(None, _CLAIMS, goal="follow_up",  # type: ignore[arg-type]
                                    intent="Follow up", first_name="Jane", visitor_ids=[])
    return result, provider


@pytest.mark.parametrize(("text", "chunks", "band", "reason"), [
    ("SUBJECT: Your roof\n\nHi Jane, we do free inspections.", ["We offer free inspections."],
     "answer", None),
    ("SUBJECT: Checking in\n\nHi Jane, just checking in.", [], "no_context", None),
    ("NO_ANSWER_FOUND", ["x"], "escalate", "NO_ANSWER_FOUND"),
    ("SUBJECT: Hi\n\nI'm a real person, promise.", ["x"], "escalate",
     "GUARDRAIL_HUMAN_IMPERSONATION"),
    ("SUBJECT: Deal\n\nTake 30% off today.", ["We install roofs."], "escalate",
     "UNSTATED_COMMITMENT"),
    ("Hi Jane, no subject line", ["x"], "escalate", "MALFORMED_OUTPUT"),
])
async def test_composer_bands(monkeypatch: pytest.MonkeyPatch, text: str, chunks: list[str],
                              band: str, reason: str | None) -> None:
    result, provider = await _compose(monkeypatch, text, chunks)
    assert (result.band, result.reason) == (band, reason)
    system = provider.prompts[0][0].content
    assert "ONLY facts stated in the Context" in system
    if band == "answer":
        assert result.sources == [{"chunk_id": "c1", "doc_id": "d1"}]


def test_footer_is_code_owned() -> None:
    body = composer.assemble_body(body="Hi Jane.", booking="Book: https://cal.com/x",
                                  company="Acme", physical_address="1 Main St, Miami FL",
                                  unsubscribe_url="https://u/1")
    assert body.endswith("Unsubscribe: https://u/1")
    assert "— Rebecca, AI assistant for Acme" in body and "1 Main St, Miami FL" in body

"""Name-first, email-later chat flow end to end on a REAL migrated Postgres:
real conversation store, leads and turn pipeline; only the LLM and retrieval
are faked (scripted classify + a generate that records each prompt)."""
from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import pytest
from common.auth import AuthClaims, Role
from common.db import Database

from api.config import get_api_settings
from api.llm.config_repository import LLMConfig
from api.llm.provider import Completion
from api.orchestrator import identity_capture as ic
from api.orchestrator import service
from api.orchestrator.config_repository import upsert_orchestrator_config
from api.rag.service import HybridMatch, HybridResult

pytestmark = pytest.mark.integration

_TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")
_DEV_DB_URL = os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_URL_DIRECT")
if not _TEST_DB_URL:
    pytest.skip("TEST_DATABASE_URL not set — skipping integration tests", allow_module_level=True)
if _DEV_DB_URL and _TEST_DB_URL == _DEV_DB_URL:
    pytest.skip("TEST_DATABASE_URL equals the dev database — refusing.", allow_module_level=True)


class _Provider:
    def __init__(self) -> None:
        self.prompts: list[Any] = []
        self.classified: list[str] = []

    async def classify(self, text: str, labels: list[str], **_: Any) -> str:
        self.classified.append(text)
        return "question"

    async def generate(self, messages: list[Any], **_: Any) -> Completion:
        self.prompts.append(messages)
        return Completion(text="A grounded answer.", model="m", input_tokens=1, output_tokens=1)

    async def aclose(self) -> None:
        return None


@pytest.fixture
async def chat(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[dict[str, Any]]:
    get_api_settings.cache_clear()
    assert _TEST_DB_URL is not None
    db = await Database.connect(_TEST_DB_URL, min_size=1, max_size=3, statement_cache_size=0)
    tenant = f"t-{uuid4().hex[:10]}"
    await db.execute("INSERT INTO client_accounts (id, name) VALUES ($1, $1)", tenant)
    await db.execute("INSERT INTO tenants (id, name, slug, enabled, client_account_id) "
                     "VALUES ($1, $1, $1, true, $1)", tenant)
    admin = AuthClaims(subject="admin", role=Role.CLIENT_ADMIN, tenant_id=tenant)
    await upsert_orchestrator_config(db, admin, answer_threshold=0.5, escalate_threshold=0.2,
                                     turn_cap=10, identity_gate_enabled=True)
    provider = _Provider()

    async def llm_config(db: Any, claims: Any) -> LLMConfig:
        return LLMConfig(provider="openai", model="m", api_key="k", embedding_model="e")

    async def retrieve(db: Any, claims: Any, query: str, *, k: int) -> HybridResult:
        return HybridResult(chunks=[HybridMatch(doc_id="d", chunk_id="c", content="Roofs.",
                                                score=0.9, rrf_score=0.5, matched_by=["vector"])],
                            confidence=0.9)

    monkeypatch.setattr(service, "get_llm_config", llm_config)
    monkeypatch.setattr(service, "retrieve_hybrid", retrieve)
    monkeypatch.setattr(service, "provider_for", lambda config: provider)
    visitor = AuthClaims(subject=f"v-{uuid4().hex[:8]}", role=Role.VISITOR, tenant_id=tenant)
    yield {"db": db, "visitor": visitor, "provider": provider, "tenant": tenant}
    await db.close()


async def test_name_first_then_answers_by_name_then_asks_for_email(chat: dict[str, Any]) -> None:
    db, visitor, provider = chat["db"], chat["visitor"], chat["provider"]

    async def say(text: str, conversation_id: str | None) -> Any:
        return await service.answer_turn(db, visitor, message=text,
                                         conversation_id=conversation_id)

    first = await say("How much does a roof repair cost?", None)
    conv = first.conversation_id
    assert first.reply == ic.ASK_NAME_REPLY and "email" not in first.reply.lower()
    assert provider.prompts == []  # no LLM spend on the name question

    second = await say("I'm jane", conv)
    assert second.decision == "answer"
    assert provider.classified[-1] == "How much does a roof repair cost?"
    prompt = provider.prompts[-1]
    assert "first name is Jane" in prompt[0].content
    assert prompt[-1].content == "My name is Jane. How much does a roof repair cost?"
    assert await db.fetchval("SELECT name FROM leads WHERE visitor_id = $1",
                             visitor.subject) == "Jane"

    await say("Do you also do gutters?", conv)  # turn 2: no email ask yet
    assert "email to reach them" not in provider.prompts[-1][0].content
    await say("How long does it take?", conv)  # turn 3: the natural email ask
    assert "email to reach them" in provider.prompts[-1][0].content

    ack = await say("sure, jane@example.com", conv)
    assert ack.reply == ic.email_ack_reply("Jane", "jane@example.com", ask_phone=True)
    assert await db.fetchval("SELECT email FROM leads WHERE visitor_id = $1",
                             visitor.subject) == "jane@example.com"

    later = await say("And warranties?", conv)  # never asked again, name still used
    assert "email to reach them" not in provider.prompts[-1][0].content
    assert ic.PHONE_ASK not in later.reply  # the number was already asked in the thank-you
    assert "first name is Jane" in provider.prompts[-1][0].content
    asks = await db.fetchval("SELECT count(*) FROM messages WHERE conversation_id = $1 "
                             "AND intent = $2", conv, ic.ASK_NAME_INTENT)
    assert asks == 1


async def test_a_visitor_who_skips_the_name_is_answered_and_not_nagged(
    chat: dict[str, Any],
) -> None:
    db, visitor, provider = chat["db"], chat["visitor"], chat["provider"]
    first = await service.answer_turn(db, visitor, message="Hi, do you fix leaks?")
    conv = first.conversation_id
    assert first.decision == "identity_gate"
    second = await service.answer_turn(db, visitor, message="Just tell me if you fix leaks?",
                                       conversation_id=conv)
    third = await service.answer_turn(db, visitor, message="What areas do you cover?",
                                      conversation_id=conv)
    assert (second.decision, third.decision) == ("answer", "answer")
    assert provider.classified == ["Just tell me if you fix leaks?", "What areas do you cover?"]
    assert await db.fetchval("SELECT count(*) FROM leads WHERE visitor_id = $1",
                             visitor.subject) == 0


async def test_email_then_number_in_chat_gives_the_calling_agent_a_consented_number(
    chat: dict[str, Any],
) -> None:
    """Email -> the thank-you asks for the number with the AI-call disclosure ->
    the number is call consent (exact wording stored) and the not-yet-started
    follow-up plan is rebuilt with a call step in the contact's timezone."""
    from api.agents import dispatch
    from api.agents.repository import AgentSettings, upsert_agent_settings

    db, visitor, tenant = chat["db"], chat["visitor"], chat["tenant"]
    admin = AuthClaims(subject="admin", role=Role.CLIENT_ADMIN, tenant_id=tenant)
    await upsert_agent_settings(db, admin, AgentSettings(
        enabled_channels=("call", "email"), inbound_auto_enabled=True,
        physical_address="1 Main St"))

    async def say(text: str, conversation_id: str | None) -> Any:
        return await service.answer_turn(db, visitor, message=text,
                                         conversation_id=conversation_id,
                                         timezone="America/New_York")

    conv = (await say("Do you fix leaks?", None)).conversation_id
    await say("Jane", conv)
    thanks = await say("jane@example.com", conv)
    assert thanks.reply.endswith(ic.PHONE_ASK)
    await dispatch.dispatch_events(db, limit=10)  # email-only plan
    assert [r["channel"] for r in await db.fetch(
        "SELECT channel FROM outreach_tasks t JOIN outreach_plans p USING (tenant_id, plan_id) "
        "WHERE p.tenant_id = $1 AND p.status = 'active'", tenant)] == ["email", "email"]

    ack = await say("(786) 823-3553", conv)
    assert ack.reply == ic.phone_ack_reply("Jane", consented=True)
    assert await db.fetchval("SELECT phone FROM leads WHERE visitor_id = $1",
                             visitor.subject) == "+17868233553"
    await dispatch.dispatch_events(db, limit=10)
    assert await db.fetchval("SELECT last_error FROM agent_events WHERE tenant_id = $1 "
                             "ORDER BY created_at DESC LIMIT 1", tenant) == "replanned"

    call = await db.fetchrow("SELECT basis, source, wording FROM contact_consents "
                             "WHERE tenant_id = $1 AND channel = 'call'", tenant)
    assert call is not None and call["wording"] == ic.PHONE_ASK
    assert (call["basis"], call["source"]) == ("express_consent", "widget")
    assert await db.fetchval("SELECT timezone FROM contacts WHERE tenant_id = $1",
                             tenant) == "America/New_York"
    channels = [r["channel"] for r in await db.fetch(
        "SELECT channel FROM outreach_tasks t JOIN outreach_plans p USING (tenant_id, plan_id) "
        "WHERE p.tenant_id = $1 AND p.status = 'active' ORDER BY step_index", tenant)]
    assert channels == ["email", "call", "email"]


async def test_a_number_typed_without_the_disclosure_is_not_call_consent(
    chat: dict[str, Any],
) -> None:
    db, visitor, tenant = chat["db"], chat["visitor"], chat["tenant"]
    conv = (await service.answer_turn(db, visitor, message="Hi")).conversation_id
    await service.answer_turn(db, visitor, message="Sam", conversation_id=conv)
    await service.answer_turn(db, visitor, message="you can reach me at 786 823 3553",
                              conversation_id=conv)
    assert await db.fetchval("SELECT phone FROM leads WHERE visitor_id = $1",
                             visitor.subject) == "+17868233553"
    assert await db.fetchval("SELECT count(*) FROM agent_events WHERE tenant_id = $1",
                             tenant) == 0

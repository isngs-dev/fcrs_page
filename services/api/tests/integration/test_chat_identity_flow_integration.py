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

from api.agents import dispatch, planner
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

    # No fixed turn: until there's an email, the model may ask when it fits (2026-10-30).
    assert ic.EMAIL_GUIDANCE in provider.prompts[-1][0].content
    await say("Do you also do gutters?", conv)
    assert ic.EMAIL_GUIDANCE in provider.prompts[-1][0].content

    ack = await say("sure, jane@example.com", conv)
    assert ack.reply == ic.email_ack_reply("Jane", "jane@example.com", ask_phone=True)
    assert await db.fetchval("SELECT email FROM leads WHERE visitor_id = $1",
                             visitor.subject) == "jane@example.com"

    later = await say("And warranties?", conv)  # never asked again, name still used
    assert ic.EMAIL_GUIDANCE not in provider.prompts[-1][0].content
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
    steps = [(r["channel"], r["intent"]) for r in await db.fetch(
        "SELECT channel, intent FROM outreach_tasks t JOIN outreach_plans p "
        "USING (tenant_id, plan_id) WHERE p.tenant_id = $1 AND p.status = 'active' "
        "ORDER BY step_index", tenant)]
    # Rebecca rings first, once the chat is over (dispatch waits for the visitor to go quiet).
    assert [c for c, _ in steps] == ["call", "email", "email"]
    assert steps[0][1] == planner.CHAT_CALL_INTENT


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


# -- Natural asks + "call when the chat ends" (2026-10-30) ---------------------------------


async def _agents_on(db: Database, tenant: str) -> None:
    from api.agents.repository import AgentSettings, upsert_agent_settings

    admin = AuthClaims(subject="admin", role=Role.CLIENT_ADMIN, tenant_id=tenant)
    await upsert_agent_settings(db, admin, AgentSettings(
        enabled_channels=("call", "email"), inbound_auto_enabled=True,
        physical_address="1 Main St", min_hours_between_touches=48))


def _sayer(db: Database, visitor: AuthClaims) -> Any:
    state: dict[str, str | None] = {"conv": None}

    async def say(text: str) -> Any:
        turn = await service.answer_turn(db, visitor, message=text,
                                         conversation_id=state["conv"],
                                         timezone="America/New_York")
        state["conv"] = turn.conversation_id
        return turn
    return say


async def test_asking_for_a_call_gets_the_number_question_with_the_disclosure(
    chat: dict[str, Any],
) -> None:
    db, visitor, tenant = chat["db"], chat["visitor"], chat["tenant"]
    say = _sayer(db, visitor)
    await say("Do you fix leaks?")
    await say("Jane")
    asked = await say("Can someone call me about a quote?")
    assert asked.reply.endswith(ic.PHONE_ASK)
    await say("(786) 823-3553")
    assert await db.fetchval("SELECT count(*) FROM agent_events WHERE tenant_id = $1 "
                             "AND kind = 'inbound_lead_created'", tenant) == 1


async def test_an_email_in_a_longer_message_is_followed_by_the_number_question(
    chat: dict[str, Any],
) -> None:
    db, visitor = chat["db"], chat["visitor"]
    say = _sayer(db, visitor)
    await say("Do you fix leaks?")
    await say("Jane")
    reply = await say("Sure, it's jane@example.com. Do you do gutters too?")
    assert reply.reply.endswith(ic.PHONE_ASK)
    assert await db.fetchval("SELECT email FROM leads WHERE visitor_id = $1",
                             visitor.subject) == "jane@example.com"


async def test_saying_goodbye_asks_once_for_what_is_missing_then_says_goodbye(
    chat: dict[str, Any],
) -> None:
    db, visitor, provider = chat["db"], chat["visitor"], chat["provider"]
    say = _sayer(db, visitor)
    await say("Do you fix leaks?")
    await say("Do you cover Miami?")  # skipped the name
    spent = len(provider.prompts)
    wrap = await say("ok thanks, that's all")
    assert wrap.reply == ic.wrap_up_reply(None, need_name=True, need_email=True,
                                          need_phone=True)
    assert wrap.reply.endswith(ic.PHONE_ASK) and len(provider.prompts) == spent

    await say("Sam Lee, sam@example.com, 786 823 3553")
    lead = await db.fetchrow("SELECT name, email, phone FROM leads WHERE visitor_id = $1",
                             visitor.subject)
    assert tuple(lead) == ("Sam Lee", "sam@example.com", "+17868233553")
    # The number after the wrap-up's disclosure is call consent.
    assert await db.fetchval(
        "SELECT count(*) FROM agent_events WHERE tenant_id = $1 AND kind = "
        "'inbound_lead_created' AND payload->'channels' = '[\"call\"]'::jsonb",
        chat["tenant"]) == 1

    bye = await say("bye")
    assert bye.reply == ic.goodbye_reply("Sam")
    again = await say("thanks, bye")  # never asked twice
    assert again.reply == ic.goodbye_reply("Sam")


async def test_a_goodbye_with_details_missing_but_already_asked_just_says_goodbye(
    chat: dict[str, Any],
) -> None:
    db, visitor = chat["db"], chat["visitor"]
    say = _sayer(db, visitor)
    await say("Do you fix leaks?")
    await say("Jane")
    await say("that's all, thanks")  # the wrap-up ask
    second = await say("bye")
    assert second.reply == ic.goodbye_reply("Jane")


async def _chat_call(db: Database, tenant: str) -> Any:
    from api.agents.outreach_repository import ClaimedTask

    row = await db.fetchrow(
        "SELECT t.tenant_id, t.task_id, t.plan_id, p.contact_id, t.channel, t.goal, t.intent, "
        "t.attempts, t.max_attempts, t.scheduled_at FROM outreach_tasks t "
        "JOIN outreach_plans p USING (tenant_id, plan_id) WHERE t.tenant_id = $1 "
        "AND p.status = 'active' AND t.channel = 'call'", tenant)
    task = ClaimedTask(tenant_id=row["tenant_id"], task_id=row["task_id"],
                       plan_id=row["plan_id"], contact_id=row["contact_id"],
                       channel=row["channel"], goal=row["goal"], intent=row["intent"],
                       attempts=row["attempts"], max_attempts=row["max_attempts"])
    return task, row["scheduled_at"]


async def test_the_chat_call_waits_for_the_visitor_to_go_quiet_or_say_goodbye(
    chat: dict[str, Any],
) -> None:
    from datetime import UTC, datetime, timedelta

    db, visitor, tenant = chat["db"], chat["visitor"], chat["tenant"]
    claims = dispatch.system_claims(tenant)
    await _agents_on(db, tenant)
    say = _sayer(db, visitor)
    await say("Do you fix leaks?")
    await say("Jane")
    await say("jane@example.com")
    await say("786 823 3553")
    await dispatch.dispatch_events(db, limit=10)
    task, _ = await _chat_call(db, tenant)
    assert task.intent == planner.CHAT_CALL_INTENT
    assert dispatch.gate_purpose(task) == "first_response"  # the email minutes ago won't hold it

    now = datetime.now(UTC)
    wait = await dispatch.chat_call_wait(db, claims, task, now)
    assert wait is not None and now < wait <= now + planner.CHAT_IDLE  # still chatting
    assert await dispatch.chat_call_wait(db, claims, task, wait + timedelta(seconds=1)) is None

    await say("great, bye!")  # goodbye: ring now, don't wait out the quiet time
    _, scheduled_at = await _chat_call(db, tenant)
    assert scheduled_at <= datetime.now(UTC)
    assert await dispatch.chat_call_wait(db, claims, task, datetime.now(UTC)) is None


async def test_another_tenants_chat_never_holds_back_this_tenants_call(
    chat: dict[str, Any],
) -> None:
    from datetime import UTC, datetime

    db, visitor, tenant = chat["db"], chat["visitor"], chat["tenant"]
    await _agents_on(db, tenant)
    say = _sayer(db, visitor)
    for text in ("Do you fix leaks?", "Jane", "jane@example.com", "786 823 3553", "bye"):
        await say(text)
    await dispatch.dispatch_events(db, limit=10)
    task, _ = await _chat_call(db, tenant)

    other = f"t-{uuid4().hex[:10]}"
    await db.execute("INSERT INTO client_accounts (id, name) VALUES ($1, $1)", other)
    await db.execute("INSERT INTO tenants (id, name, slug, enabled, client_account_id) "
                     "VALUES ($1, $1, $1, true, $1)", other)
    same_visitor = AuthClaims(subject=visitor.subject, role=Role.VISITOR, tenant_id=other)
    await service.answer_turn(db, same_visitor, message="Hello, are you open today?")

    assert await dispatch.chat_call_wait(db, dispatch.system_claims(tenant), task,
                                         datetime.now(UTC)) is None


async def test_a_number_given_after_the_first_email_went_out_still_gets_a_call(
    chat: dict[str, Any],
) -> None:
    db, visitor, tenant = chat["db"], chat["visitor"], chat["tenant"]
    await _agents_on(db, tenant)
    say = _sayer(db, visitor)
    await say("Do you fix leaks?")
    await say("Jane")
    await say("jane@example.com")
    await dispatch.dispatch_events(db, limit=10)  # email-only plan
    await db.execute("UPDATE outreach_tasks SET status = 'completed', completed_at = now() "
                     "WHERE tenant_id = $1 AND step_index = 0", tenant)
    await say("Can you call me instead?")
    await say("786 823 3553")
    await dispatch.dispatch_events(db, limit=10)
    task, _ = await _chat_call(db, tenant)
    assert task.intent == planner.CHAT_CALL_INTENT

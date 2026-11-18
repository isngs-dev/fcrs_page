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
        self.label = "question"

    async def classify(self, text: str, labels: list[str], **_: Any) -> str:
        self.classified.append(text)
        return self.label

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

    # 2026-11-04: their first answered question ends with our own email ask (the model's
    # was too easy to skip), and that turn the model isn't told to ask as well.
    assert second.reply == "A grounded answer.\n\n" + ic.EMAIL_ASK
    assert ic.EMAIL_GUIDANCE not in provider.prompts[-1][0].content
    gutters = await say("Do you also do gutters?", conv)
    assert ic.EMAIL_ASK not in gutters.reply  # asked once
    # Live 2026-11-18: and the model isn't told to ask again either (it did, two turns on).
    assert ic.EMAIL_GUIDANCE not in provider.prompts[-1][0].content

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
    await dispatch.dispatch_events(db, limit=500)  # email-only plan
    assert [r["channel"] for r in await db.fetch(
        "SELECT channel FROM outreach_tasks t JOIN outreach_plans p USING (tenant_id, plan_id) "
        "WHERE p.tenant_id = $1 AND p.status = 'active'", tenant)] == ["email"] * 3  # day 0/2/7

    ack = await say("(786) 823-3553", conv)
    assert ack.reply == ic.phone_ack_reply("Jane", consented=True)
    assert await db.fetchval("SELECT phone FROM leads WHERE visitor_id = $1",
                             visitor.subject) == "+17868233553"
    await dispatch.dispatch_events(db, limit=500)
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
    assert [c for c, _ in steps] == ["call", "email", "email", "call", "email"]  # plan 1.4: day 0/2/4/7
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
        "AND p.status = 'active' AND t.channel = 'call' "
        "ORDER BY t.step_index LIMIT 1", tenant)  # plan 1.4: the day-0 call, not day 4's
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
    await dispatch.dispatch_events(db, limit=500)
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
    await dispatch.dispatch_events(db, limit=500)
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
    await dispatch.dispatch_events(db, limit=500)  # email-only plan
    await db.execute("UPDATE outreach_tasks SET status = 'completed', completed_at = now() "
                     "WHERE tenant_id = $1 AND step_index = 0", tenant)
    await say("Can you call me instead?")
    await say("786 823 3553")
    await dispatch.dispatch_events(db, limit=500)
    task, _ = await _chat_call(db, tenant)
    assert task.intent == planner.CHAT_CALL_INTENT


async def _dialled(db: Database, tenant: str) -> str | None:
    """The number the calling agent would ring for the chat call's contact."""
    from api.call_agent.service import _contact_phone

    task, _ = await _chat_call(db, tenant)
    return await _contact_phone(db, dispatch.system_claims(tenant), task.contact_id)


async def test_a_number_given_after_the_email_is_the_one_rebecca_dials(
    chat: dict[str, Any],
) -> None:
    """Live 2026-11-02: email first (the contact is made then, with no number), number
    second -> saved on the lead only, so the call had nothing to ring."""
    db, visitor, tenant = chat["db"], chat["visitor"], chat["tenant"]
    await _agents_on(db, tenant)
    say = _sayer(db, visitor)
    for text in ("How much does it cost?", "Sainath", "sainath@example.com"):
        await say(text)
    await dispatch.dispatch_events(db, limit=500)  # contact made now, without a number
    await say("+918828393252")
    await dispatch.dispatch_events(db, limit=500)
    assert await _dialled(db, tenant) == "+918828393252"


async def test_a_returning_visitor_is_rung_on_the_number_they_just_gave(
    chat: dict[str, Any],
) -> None:
    """Same email as an earlier contact: the chat joins that contact, whose old number
    must not be the one rung. Another tenant's contact with the same email is untouched."""
    from api.contacts.repository import add_identity, create_contact

    db, visitor, tenant = chat["db"], chat["visitor"], chat["tenant"]
    await _agents_on(db, tenant)
    admin = AuthClaims(subject="admin", role=Role.CLIENT_ADMIN, tenant_id=tenant)
    old = await create_contact(db, admin, account_id=None, lead_id=None, name="Sainath",
                               email="sainath@example.com", phone="+917000000001", consent={})
    await add_identity(db, admin, old, identity_type="email", identity_value="sainath@example.com")
    await add_identity(db, admin, old, identity_type="phone", identity_value="+917000000001")
    other = f"t-{uuid4().hex[:10]}"
    await db.execute("INSERT INTO client_accounts (id, name) VALUES ($1, $1)", other)
    await db.execute("INSERT INTO tenants (id, name, slug, enabled, client_account_id) "
                     "VALUES ($1, $1, $1, true, $1)", other)
    elsewhere = await create_contact(
        db, AuthClaims(subject="admin", role=Role.CLIENT_ADMIN, tenant_id=other),
        account_id=None, lead_id=None, name="Sainath", email="sainath@example.com",
        phone="+917000000001", consent={})

    say = _sayer(db, visitor)
    for text in ("How much does it cost?", "Sainath", "sainath@example.com"):
        await say(text)
    await dispatch.dispatch_events(db, limit=500)
    await say("+918828393252")
    await dispatch.dispatch_events(db, limit=500)
    task, _ = await _chat_call(db, tenant)
    assert task.contact_id == old  # joined by email, as before
    assert await _dialled(db, tenant) == "+918828393252"
    assert await db.fetchval("SELECT phone FROM contacts WHERE tenant_id = $1 AND "
                             "contact_id = $2", other, elsewhere) == "+917000000001"


# -- "Just email me" (2026-11-03) -------------------------------------------------------------


async def _active_steps(db: Database, tenant: str) -> list[tuple[str, str]]:
    return [(r["channel"], r["status"]) for r in await db.fetch(
        "SELECT t.channel, t.status FROM outreach_tasks t JOIN outreach_plans p "
        "USING (tenant_id, plan_id) WHERE p.tenant_id = $1 AND p.status = 'active' "
        "ORDER BY t.step_index", tenant)]


async def test_just_email_me_starts_the_email_follow_up_and_stops_asking_for_a_number(
    chat: dict[str, Any],
) -> None:
    """A real chat: "No just email me" after the number question got "I can't send an email
    from this chat", and "Just follow-up through email" got the hand-off line."""
    db, visitor, tenant, provider = chat["db"], chat["visitor"], chat["tenant"], chat["provider"]
    await _agents_on(db, tenant)
    say = _sayer(db, visitor)
    for text in ("Do you fix leaks?", "Jane"):
        await say(text)
    assert (await say("jane@example.com")).reply.endswith(ic.PHONE_ASK)
    spent = len(provider.prompts)

    for text in ("No just email me", "Just follow-up through email"):
        reply = await say(text)
        assert reply.reply == ic.email_only_reply("Jane", "jane@example.com")
        assert reply.decision == "answer"
    assert len(provider.prompts) == spent  # no model reply, so no "I can't email you"
    await dispatch.dispatch_events(db, limit=500)
    assert await _active_steps(db, tenant) == [("email", "scheduled")] * 3  # day 0/2/7
    assert await db.fetchval("SELECT count(*) FROM outreach_plans WHERE tenant_id = $1",
                             tenant) == 1

    bye = await say("ok thanks, that's all")  # no "before you go" number ask
    assert bye.reply == ic.goodbye_reply("Jane")


async def test_dont_call_me_calls_off_a_planned_call_and_keeps_the_emails(
    chat: dict[str, Any],
) -> None:
    db, visitor, tenant = chat["db"], chat["visitor"], chat["tenant"]
    await _agents_on(db, tenant)
    say = _sayer(db, visitor)
    for text in ("Do you fix leaks?", "Jane", "jane@example.com", "(786) 823-3553"):
        await say(text)
    await dispatch.dispatch_events(db, limit=500)
    assert [c for c, _ in await _active_steps(db, tenant)] == ["call", "email", "email", "call", "email"]

    await say("Please don't call me")
    await dispatch.dispatch_events(db, limit=500)
    assert await _active_steps(db, tenant) == [
        ("call", "cancelled"), ("email", "scheduled"), ("email", "scheduled"),
        ("call", "cancelled"), ("email", "scheduled")]  # both calls off, emails kept
    assert await db.fetchval(
        "SELECT count(*) FROM suppression_list WHERE tenant_id = $1 AND channel = 'call' "
        "AND value_normalised = '+17868233553'", tenant) == 1


async def test_email_only_before_an_email_asks_for_the_email_then_never_for_a_number(
    chat: dict[str, Any],
) -> None:
    db, visitor, tenant = chat["db"], chat["visitor"], chat["tenant"]
    await _agents_on(db, tenant)
    say = _sayer(db, visitor)
    for text in ("Do you fix leaks?", "Jane"):
        await say(text)
    assert (await say("Just email me please")).reply == ic.email_only_reply("Jane", None)
    ack = await say("jane@example.com")
    assert ack.reply == ic.email_ack_reply("Jane", "jane@example.com", ask_phone=False)
    await dispatch.dispatch_events(db, limit=500)
    assert [c for c, _ in await _active_steps(db, tenant)] == ["email"] * 3


# -- The email ask after an answer (2026-11-04) ---------------------------------------------


async def test_the_first_answered_question_asks_for_the_email_and_chitchat_does_not(
    chat: dict[str, Any],
) -> None:
    """A real chat answered two questions and never asked for an email."""
    db, visitor, provider = chat["db"], chat["visitor"], chat["provider"]
    say = _sayer(db, visitor)
    await say("Is it just Google and Facebook advertising?")
    provider.label = "chitchat"
    hello = await say("Aastha")
    assert ic.EMAIL_ASK not in hello.reply
    provider.label = "question"
    answered = await say("So what's included beyond the advertising?")
    assert answered.reply.endswith(ic.EMAIL_ASK)
    assert await db.fetchval("SELECT count(*) FROM messages WHERE conversation_id = $1 "
                             "AND intent = $2", answered.conversation_id, ic.ASK_EMAIL_INTENT) == 1
    ack = await say("aastha@example.com")
    assert ack.reply == ic.email_ack_reply("Aastha", "aastha@example.com", ask_phone=True)


# -- Qualification in the chat (plan 1.2, owner 2026-11-17) ----------------------------------


async def _chatted_and_known(chat: dict[str, Any]) -> Any:
    """Name, email and number given; the agents have made their contact."""
    db, visitor, tenant = chat["db"], chat["visitor"], chat["tenant"]
    await _agents_on(db, tenant)
    say = _sayer(db, visitor)
    for text in ("Do you fix leaks?", "Jane", "jane@example.com", "786 823 3553"):
        await say(text)
    await dispatch.dispatch_events(db, limit=500)
    return say


async def test_the_chat_asks_two_missing_questions_one_at_a_time_after_an_answer(
    chat: dict[str, Any],
) -> None:
    from api.contacts.repository import get_contact_id_by_identity
    from api.qualification import repository as qrepo

    db, visitor, tenant, provider = chat["db"], chat["visitor"], chat["tenant"], chat["provider"]
    claims = dispatch.system_claims(tenant)
    say = await _chatted_and_known(chat)
    cid = await get_contact_id_by_identity(db, claims, identity_type="visitor_id",
                                           identity_value=visitor.subject)
    assert cid is not None
    # Scored as soon as the agents have them: chatted 20 + asked for a call 10, x 10%.
    assert (await qrepo.get_record(db, claims, cid)).score == 3

    first = await say("Do you do gutters too?")
    assert first.reply == ("A grounded answer.\n\nIf you don't mind me asking, roughly how many "
                           "roofing jobs do you do a month?")
    provider.label = "5 to 20"  # the model maps their answer to a choice
    thanks = await say("about 15 a month")
    assert thanks.reply == ic.QUAL_THANKS_REPLY
    record = await qrepo.get_record(db, claims, cid)
    assert record.chosen == {"jobs_per_month": "5 to 20"}
    assert record.answers["jobs_per_month"]["channel"] == "chat"

    provider.label = "question"
    second = await say("Do you cover Miami?")
    assert second.reply.endswith("If you don't mind me asking, what do you spend on marketing "
                                 "a month?")
    provider.label = "$1k to $5k"
    await say("around 3k")
    provider.label = "question"
    third = await say("And how long does a campaign take?")
    assert third.reply == "A grounded answer."  # two a conversation, never more
    assert (await qrepo.get_record(db, claims, cid)).chosen == {
        "jobs_per_month": "5 to 20", "marketing_spend": "$1k to $5k"}


async def test_a_question_back_instead_of_an_answer_is_answered_and_not_saved(
    chat: dict[str, Any],
) -> None:
    from api.contacts.repository import get_contact_id_by_identity
    from api.qualification import repository as qrepo

    db, visitor, tenant = chat["db"], chat["visitor"], chat["tenant"]
    claims = dispatch.system_claims(tenant)
    say = await _chatted_and_known(chat)
    await say("Do you do gutters too?")
    back = await say("Why do you need to know that?")
    assert back.reply == "A grounded answer."
    cid = await get_contact_id_by_identity(db, claims, identity_type="visitor_id",
                                           identity_value=visitor.subject)
    assert (await qrepo.get_record(db, claims, str(cid))).answers == {}


async def test_no_question_is_asked_before_there_is_a_contact_to_keep_the_answer(
    chat: dict[str, Any],
) -> None:
    """Agents off: no contact is made, so nothing could be saved -- nothing is asked."""
    db, visitor = chat["db"], chat["visitor"]
    say = _sayer(db, visitor)
    for text in ("Do you fix leaks?", "Jane", "jane@example.com", "786 823 3553"):
        await say(text)
    reply = await say("Do you do gutters too?")
    assert reply.reply == "A grounded answer."


# -- Readiness: offer a time when they're ready, never push explorers (plan 1.5) -------------


async def _detailed_visitor(chat: dict[str, Any]) -> Any:
    """Name, email and number given; agents off, so no qualification questions get in the way."""
    say = _sayer(chat["db"], chat["visitor"])
    for text in ("Do you fix leaks?", "Jane", "jane@example.com", "786 823 3553"):
        await say(text)
    return say


async def test_a_ready_visitor_is_offered_a_time_once(chat: dict[str, Any]) -> None:
    say = await _detailed_visitor(chat)
    offered = await say("How do I get started with you?")
    assert offered.reply == f"A grounded answer.\n\n{ic.BOOKING_OFFER}"
    again = await say("Can you send me a quote?")
    assert again.reply == "A grounded answer."  # offered once a conversation


async def test_yes_to_the_offer_opens_the_card_filled_in_without_asking_again(
    chat: dict[str, Any],
) -> None:
    say = await _detailed_visitor(chat)
    await say("When can you start?")
    yes = await say("yes please")
    assert yes.action in ("schedule_cta", "lead_form")  # the card, pre-filled
    assert yes.prefill is not None and {k: yes.prefill[k] for k in ("name", "email", "phone")} == {
        "name": "Jane", "email": "jane@example.com", "phone": "+17868233553"}
    assert "share your name" not in yes.reply  # nothing they already gave is asked again


async def test_no_to_the_offer_is_respected(chat: dict[str, Any]) -> None:
    say = await _detailed_visitor(chat)
    await say("How do I get started with you?")
    no = await say("no thanks, not yet")
    assert no.reply == ic.BOOKING_OFFER_DECLINED_REPLY and no.action is None


async def test_an_exploring_visitor_is_answered_and_never_pushed(chat: dict[str, Any]) -> None:
    say = await _detailed_visitor(chat)
    for question in ("Do you do gutters?", "What areas do you cover?",
                     "How much does SEO usually cost?"):
        reply = await say(question)
        assert reply.reply == "A grounded answer." and reply.action is None


async def test_without_details_yes_asks_only_for_what_is_missing(chat: dict[str, Any]) -> None:
    db, visitor = chat["db"], chat["visitor"]
    say = _sayer(db, visitor)
    await say("Do you fix leaks?")
    await say("Jane")
    offered = await say("How do I get started with you?")
    assert offered.reply.endswith(ic.BOOKING_OFFER)
    yes = await say("yes")
    assert yes.action is None and "email and phone number" in yes.reply
    assert "name" not in yes.reply.split("share")[-1]  # her name is already known


# -- Live chat 2026-11-18: the email asked twice; "Nothing" not heard as a goodbye ------------


async def test_once_the_email_is_asked_for_the_model_is_not_told_to_ask_again(
    chat: dict[str, Any],
) -> None:
    db, visitor, provider = chat["db"], chat["visitor"], chat["provider"]
    say = _sayer(db, visitor)
    await say("Do you fix leaks?")
    answered = await say("Vinith")  # the deferred question, answered with our own email ask
    assert answered.reply.endswith(ic.EMAIL_ASK)
    await say("So how can roofengine help me get roofers?")
    assert ic.EMAIL_GUIDANCE not in provider.prompts[-1][0].content  # no second ask from it


async def test_nothing_after_what_else_is_a_goodbye_and_rings_the_waiting_call(
    chat: dict[str, Any],
) -> None:
    db, visitor, tenant = chat["db"], chat["visitor"], chat["tenant"]
    await _agents_on(db, tenant)
    say = _sayer(db, visitor)
    for text in ("Do you fix leaks?", "Jane", "jane@example.com"):
        await say(text)
    thanks = await say("786 823 3553")
    assert thanks.reply.endswith("What else can I help you with?")
    bye = await say("Nothing")
    assert bye.reply == ic.goodbye_reply("Jane")


async def test_a_short_no_after_what_else_is_a_goodbye_but_not_after_the_offer(
    chat: dict[str, Any],
) -> None:
    say = await _detailed_visitor(chat)
    no_more = await say("no thanks")  # after "What else can I help you with?"
    assert no_more.reply == ic.goodbye_reply("Jane")
    say = await _detailed_visitor({**chat, "visitor": AuthClaims(
        subject=f"v-{uuid4().hex[:8]}", role=Role.VISITOR, tenant_id=chat["tenant"])})
    await say("How do I get started with you?")  # ends with the booking offer
    declined = await say("no thanks")
    assert declined.reply == ic.BOOKING_OFFER_DECLINED_REPLY  # the offer's own "no"


# -- One person per phone number (plan 1.1, owner 2026-11-17: "same person") -----------------


async def _known_by_phone(db: Database, tenant: str) -> str:
    """A contact from an earlier website form: name, email, number and a note."""
    from api.agents import memory
    from api.contacts.repository import add_identity, create_contact

    admin = AuthClaims(subject="admin", role=Role.CLIENT_ADMIN, tenant_id=tenant)
    old = await create_contact(db, admin, account_id=None, lead_id=None, name="Kaustubh",
                               email="kaustubh@example.com", phone="+17868233553", consent={})
    await add_identity(db, admin, old, identity_type="email",
                       identity_value="kaustubh@example.com")
    await add_identity(db, admin, old, identity_type="phone", identity_value="+17868233553")
    await memory.remember(db, admin, old, channel="web_form", kind="need",
                          text="service: meta_ads")
    return old


async def _contact_ids(db: Database, tenant: str) -> list[str]:
    return [r["contact_id"] for r in await db.fetch(
        "SELECT contact_id FROM contacts WHERE tenant_id = $1", tenant)]


async def test_a_chat_number_already_on_a_contact_joins_that_contact(
    chat: dict[str, Any],
) -> None:
    """Demo 2026-11-17: the chat made a new contact at the email, then the number turned out
    to be an earlier form contact's -> two contacts on one phone, two sets of call limits.
    Now the chat's contact folds into the earlier one; the newest name and email win and
    the old email still finds them."""
    from datetime import UTC, datetime

    from api.contacts.repository import get_contact_by_lead_id, get_contact_id_by_identity

    db, visitor, tenant = chat["db"], chat["visitor"], chat["tenant"]
    claims = dispatch.system_claims(tenant)
    await _agents_on(db, tenant)
    old = await _known_by_phone(db, tenant)
    say = _sayer(db, visitor)
    for text in ("Do you fix leaks?", "Vinith", "vinith@example.com"):
        await say(text)
    await dispatch.dispatch_events(db, limit=500)  # the chat's own contact, no number yet
    assert len(await _contact_ids(db, tenant)) == 2
    from api.agents import memory

    chat_contact = next(c for c in await _contact_ids(db, tenant) if c != old)
    await memory.remember(db, claims, chat_contact, channel="chat", kind="need",
                          text="Asked about leak repairs")
    await say("786 823 3553")
    await dispatch.dispatch_events(db, limit=500)

    assert await _contact_ids(db, tenant) == [old]
    task, _ = await _chat_call(db, tenant)
    assert task.contact_id == old and task.intent == planner.CHAT_CALL_INTENT
    row = await db.fetchrow("SELECT name, email, phone, lead_id FROM contacts "
                            "WHERE tenant_id = $1 AND contact_id = $2", tenant, old)
    lead_id = await db.fetchval("SELECT lead_id FROM leads WHERE visitor_id = $1",
                                visitor.subject)
    assert tuple(row) == ("Vinith", "vinith@example.com", "+17868233553", lead_id)
    for email in ("vinith@example.com", "kaustubh@example.com"):
        assert await get_contact_id_by_identity(db, claims, identity_type="email",
                                                identity_value=email) == old
    assert (await get_contact_by_lead_id(db, claims, lead_id)).contact_id == old  # type: ignore[union-attr]
    # The chat's consents and the form's note are both on the one contact now.
    assert await db.fetchval("SELECT wording FROM contact_consents WHERE tenant_id = $1 "
                             "AND contact_id = $2 AND channel = 'call'", tenant, old) == ic.PHONE_ASK
    assert [i["text"] for i in await memory.items(db, claims, old)] == [
        "service: meta_ads", "Asked about leak repairs"]
    # Still waits for the visitor to go quiet: the chat's visitor id moved with them.
    assert await dispatch.chat_call_wait(db, claims, task, datetime.now(UTC)) is not None


async def test_a_new_lead_on_a_known_number_is_that_contact_and_other_tenants_are_untouched(
    chat: dict[str, Any],
) -> None:
    """Email and number in one message: no new contact; another tenant's contact on the same
    number is neither joined nor changed."""
    db, visitor, tenant = chat["db"], chat["visitor"], chat["tenant"]
    await _agents_on(db, tenant)
    old = await _known_by_phone(db, tenant)
    other = f"t-{uuid4().hex[:10]}"
    await db.execute("INSERT INTO client_accounts (id, name) VALUES ($1, $1)", other)
    await db.execute("INSERT INTO tenants (id, name, slug, enabled, client_account_id) "
                     "VALUES ($1, $1, $1, true, $1)", other)
    elsewhere = await _known_by_phone(db, other)
    say = _sayer(db, visitor)
    for text in ("Do you fix leaks?", "ok thanks, that's all",
                 "Sam Lee, sam@example.com, 786 823 3553"):
        await say(text)
    await dispatch.dispatch_events(db, limit=500)

    assert await _contact_ids(db, tenant) == [old]
    assert tuple(await db.fetchrow("SELECT name, email FROM contacts WHERE contact_id = $1",
                                   old)) == ("Sam Lee", "sam@example.com")
    assert await _contact_ids(db, other) == [elsewhere]
    assert tuple(await db.fetchrow("SELECT name, email FROM contacts WHERE contact_id = $1",
                                   elsewhere)) == ("Kaustubh", "kaustubh@example.com")


async def test_a_chat_contact_that_was_already_emailed_is_not_folded_in(
    chat: dict[str, Any],
) -> None:
    """Folding is only for a contact made moments ago by this chat with nothing sent yet:
    one with history keeps it (it is rung on the number they just gave, as before)."""
    db, visitor, tenant = chat["db"], chat["visitor"], chat["tenant"]
    await _agents_on(db, tenant)
    old = await _known_by_phone(db, tenant)
    say = _sayer(db, visitor)
    for text in ("Do you fix leaks?", "Vinith", "vinith@example.com"):
        await say(text)
    await dispatch.dispatch_events(db, limit=500)
    await db.execute("UPDATE outreach_tasks SET status = 'completed', completed_at = now() "
                     "WHERE tenant_id = $1 AND step_index = 0", tenant)
    await say("786 823 3553")
    await dispatch.dispatch_events(db, limit=500)

    ids = await _contact_ids(db, tenant)
    assert len(ids) == 2 and old in ids
    task, _ = await _chat_call(db, tenant)
    assert task.contact_id != old
    assert await _dialled(db, tenant) == "+17868233553"

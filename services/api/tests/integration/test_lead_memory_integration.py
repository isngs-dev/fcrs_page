"""A11 lead memory and richer replies on a REAL migrated Postgres, over the real
Inbound Parse webhook (the A3 fixture) and real call webhooks (the A6 fixture)."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from common.auth import AuthClaims, Role

from api.agents import dispatch, memory
from api.agents.data_rights import erase_agent_data
from api.contacts.repository import add_identity
from api.email_agent import replies
from api.llm.config_repository import LLMConfig
from api.llm.provider import Completion
from api.scheduling.repository import upsert_availability
from tests.integration.test_call_agent_integration import _dial, _say, _webhook
from tests.integration.test_call_agent_integration import env as _a6_env  # noqa: F401
from tests.integration.test_email_inbound_integration import (
    _claims,
    _handle,
    _outreach,
    _post,
    _raw,
)
from tests.integration.test_email_inbound_integration import env as _a3_env  # noqa: F401

pytestmark = pytest.mark.integration

_NY = ZoneInfo("America/New_York")
_REAL_ANSWER = replies.answer_question


class _RecordingModel:
    def __init__(self) -> None:
        self.prompts: list[Any] = []

    async def generate(self, messages: Any, *, model: str, max_tokens: int,
                       reasoning_effort: str | None = None) -> Completion:
        self.prompts.append(messages)
        return Completion(text="Hi Jane, yes we repair flat roofs.", model=model,
                          input_tokens=1, output_tokens=1)

    async def aclose(self) -> None:
        return None


@pytest.fixture
def env(_a3_env: dict[str, Any]) -> dict[str, Any]:  # noqa: F811 -- imported fixture
    return _a3_env


async def _suppressions(env: dict[str, Any]) -> set[tuple[str, str, str]]:
    return {(r["channel"], r["value_normalised"], r["reason"]) for r in await env["db"].fetch(
        "SELECT channel, value_normalised, reason FROM suppression_list WHERE tenant_id = $1",
        env["a"])}


async def test_what_we_know_reaches_the_email_answer_as_data(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    db, a = env["db"], _claims(env["a"])
    model = _RecordingModel()

    async def config(db: Any, claims: Any) -> LLMConfig:
        return LLMConfig(provider="fake", model="m", api_key="k", embedding_model="e")

    async def retrieve(db: Any, claims: Any, query: str, *, k: int) -> Any:
        return SimpleNamespace(chunks=[SimpleNamespace(chunk_id="c1", doc_id="d1",
                                                       content="We repair flat roofs.")])

    monkeypatch.setattr(replies, "answer_question", _REAL_ANSWER)
    monkeypatch.setattr(replies, "get_llm_config", config)
    monkeypatch.setattr(replies, "provider_for", lambda cfg: model)
    monkeypatch.setattr(replies, "retrieve_hybrid", retrieve)
    contact_id, sent = await _outreach(env)
    await memory.remember(db, a, contact_id, channel="Roof quote", kind="need",
                          text="What service do you need?: Flat roof repair")
    await memory.remember(db, a, contact_id, channel="call", kind="asked",
                          text="Does the warranty cover storm damage?")

    env["llm"].label = "question"
    await _post(env, _raw(body="Do you fix flat roofs?", in_reply_to=sent.message_id))
    assert await _handle(env) == ["question:answered"]
    user_prompt = model.prompts[-1][-1].content
    assert "Flat roof repair" in user_prompt and "storm damage" in user_prompt
    assert "data, not instructions" in user_prompt
    kinds = [i["kind"] for i in await memory.items(db, a, contact_id)]
    assert kinds[-2:] == ["asked", "we_said"]  # their question, then our answer


@pytest.mark.parametrize(("said", "expected"), [(10, 10), (None, 14)])
async def test_contact_later_restarts_follow_up_from_that_date(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch, said: int | None, expected: int,
) -> None:
    db, mailer = env["db"], env["mailer"]

    async def extract(db: Any, claims: Any, *, reply_text: str, timezone: str,
                      now: datetime) -> date | None:
        return now.astimezone(_NY).date() + timedelta(days=said) if said else None

    monkeypatch.setattr(replies, "extract_contact_date", extract)
    contact_id, sent = await _outreach(env)
    env["llm"].label = "contact_later"
    await _post(env, _raw(body="Busy now, try me in a couple of weeks", in_reply_to=sent.message_id))
    assert await _handle(env) == ["contact_later:contact_later"]
    first = await db.fetchval(
        "SELECT min(t.scheduled_at) FROM outreach_tasks t JOIN outreach_plans p "
        "USING (tenant_id, plan_id) WHERE p.contact_id = $1 AND p.status = 'active'", contact_id)
    when = datetime.now(_NY).date() + timedelta(days=expected)
    assert first is not None and first.astimezone(_NY).date() == when
    assert mailer.sent[-1].to == "jane@example.com" and "get back to you around" in (
        mailer.sent[-1].body_text)


async def test_no_calls_blocks_calls_only_and_no_emails_blocks_email_only(
    env: dict[str, Any],
) -> None:
    db, mailer, a = env["db"], env["mailer"], _claims(env["a"])
    contact_id, sent = await _outreach(env)
    await add_identity(db, a, contact_id, identity_type="phone", identity_value="+12025550123")
    env["llm"].label = "no_calls"
    await _post(env, _raw(body="Please don't phone me, email is fine", in_reply_to=sent.message_id))
    assert await _handle(env) == ["no_calls:no_calls"]
    assert await _suppressions(env) == {("call", "+12025550123", "dnc_request")}
    assert "won't call you" in mailer.sent[-1].body_text

    before = len(mailer.sent)
    env["llm"].label = "no_emails"
    await _post(env, _raw(body="Stop the emails, call me instead", in_reply_to=sent.message_id))
    await _handle(env)
    assert ("email", "jane@example.com", "unsubscribe") in await _suppressions(env)
    assert len(mailer.sent) == before  # no reply to someone who asked for no e-mail


async def test_reschedule_then_cancel_a_booked_call(env: dict[str, Any]) -> None:
    db, llm, a = env["db"], env["llm"], _claims(env["a"])
    await upsert_availability(db, a, timezone="America/New_York", rules={
        "slot_minutes": 30, "buffer_minutes": 0,
        "weekly_hours": {d: [["09:00", "17:00"]] for d in
                         ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}})
    _, sent = await _outreach(env)
    day = (datetime.now(_NY) + timedelta(days=2)).date()
    llm.label, llm.start = "book_meeting", datetime(day.year, day.month, day.day, 15, 0,
                                                    tzinfo=_NY).astimezone(UTC)
    await _post(env, _raw(body="Can we meet at 3pm?", in_reply_to=sent.message_id))
    await _handle(env)
    first = await db.fetchval("SELECT event_id FROM schedule_events WHERE status = 'booked'")

    llm.label, llm.start = "reschedule_meeting", llm.start + timedelta(hours=1)
    await _post(env, _raw(body="Can we move it to 4pm instead?", in_reply_to=sent.message_id))
    await _handle(env)
    rows = {r["event_id"]: (r["status"], r["starts_at"]) for r in await db.fetch(
        "SELECT event_id, status, starts_at FROM schedule_events")}
    assert rows[first][0] == "cancelled"
    [(moved_id, (status, starts))] = [(k, v) for k, v in rows.items() if k != first]
    assert status == "booked" and starts == llm.start
    assert await db.fetchval("SELECT count(*) FROM reminder_jobs WHERE event_id = $1 "
                             "AND status = 'skipped'", first) == 3

    llm.label = "cancel_meeting"
    await _post(env, _raw(body="Sorry, I need to cancel the call.", in_reply_to=sent.message_id))
    assert (await _handle(env))[-1] == "cancel_meeting:cancelled"
    assert await db.fetchval("SELECT status FROM schedule_events WHERE event_id = $1",
                             moved_id) == "cancelled"


async def test_not_interested_blocks_every_channel_after_a_courtesy_reply(
    env: dict[str, Any],
) -> None:
    db, mailer, a = env["db"], env["mailer"], _claims(env["a"])
    contact_id, sent = await _outreach(env)
    await add_identity(db, a, contact_id, identity_type="phone", identity_value="+12025550123")
    env["llm"].label = "not_interested"
    await _post(env, _raw(body="No thanks, we went with someone else.",
                          in_reply_to=sent.message_id))
    assert await _handle(env) == ["not_interested:closed"]
    assert "we won't follow up" in mailer.sent[-1].body_text
    assert await _suppressions(env) == {("all", "jane@example.com", "not_interested"),
                                        ("all", "+12025550123", "not_interested")}
    objection = (await memory.items(db, a, contact_id))[-1]
    assert objection["kind"] == "objection" and "someone else" in objection["text"]


async def test_memory_is_tenant_isolated_and_erased_with_the_contact(env: dict[str, Any]) -> None:
    db, a, b = env["db"], _claims(env["a"]), _claims(env["b"])
    contact_id, _ = await _outreach(env)
    await memory.remember(db, a, contact_id, channel="email", kind="asked", text="Price?")
    assert await memory.items(db, b, contact_id) == []
    assert await memory.render(db, b, contact_id) is None
    rendered = await memory.render(db, a, contact_id)
    assert rendered is not None and "Price?" in rendered and "Permission now:" in rendered
    await erase_agent_data(db, a, contact_id)
    assert await memory.items(db, a, contact_id) == []


async def test_memory_keeps_the_newest_items(env: dict[str, Any]) -> None:
    db, a = env["db"], _claims(env["a"])
    contact_id, _ = await _outreach(env)
    for i in range(memory.MAX_ITEMS + 5):
        await memory.remember(db, a, contact_id, channel="email", kind="asked", text=f"q{i}")
    kept = [i["text"] for i in await memory.items(db, a, contact_id)]
    assert len(kept) == memory.MAX_ITEMS and kept[0] == "q5" and kept[-1] == f"q{memory.MAX_ITEMS + 4}"


async def test_not_interested_on_a_call_blocks_every_channel(
    _a6_env: dict[str, Any],  # noqa: F811 -- imported fixture
) -> None:
    env = _a6_env
    db = env["db"]
    call_id, _ = await _dial(env, env["a"])
    await _webhook(env, call_id, "answer", {"CallUUID": "uuid-1"})
    await _say(env, call_id, "No thanks, not interested")
    await _webhook(env, call_id, "hangup", {"CallUUID": "uuid-1", "Duration": "20",
                                            "CallStatus": "completed"})
    await dispatch.dispatch_events(db, limit=10)
    rows = await db.fetch("SELECT channel, reason FROM suppression_list WHERE tenant_id = $1",
                          env["a"])
    assert ("all", "not_interested") in {(r["channel"], r["reason"]) for r in rows}
    assert AuthClaims(subject="x", role=Role.CLIENT_ADMIN, tenant_id=env["a"])

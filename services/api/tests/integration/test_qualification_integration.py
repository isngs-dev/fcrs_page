"""Plan 1.2/1.3 on a REAL migrated Postgres: per-client qualification questions, answers
mapped to a configured choice, the deterministic score + band, and the one-time hot-lead
alert to the missed-call list (owner, 2026-11-17). Tenant isolation throughout."""
from __future__ import annotations

import os
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from common.auth import AuthClaims, Role
from common.db import Database

from api.call_agent import repository as call_repo
from api.contacts.repository import create_contact
from api.qualification import repository as qrepo
from api.qualification import service
from api.qualification.scoring import DEFAULT_CONFIG, QualConfig

pytestmark = pytest.mark.integration

_TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")
_DEV_DB_URL = os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_URL_DIRECT")
if not _TEST_DB_URL:
    pytest.skip("TEST_DATABASE_URL not set — skipping integration tests", allow_module_level=True)
if _DEV_DB_URL and _TEST_DB_URL == _DEV_DB_URL:
    pytest.skip("TEST_DATABASE_URL equals the dev database — refusing.", allow_module_level=True)

_TOP = {"jobs_per_month": "over 20", "marketing_spend": "over $5k", "decision_maker": "yes",
        "improve": "more leads", "start": "this month"}


@pytest.fixture
async def env(monkeypatch: pytest.MonkeyPatch) -> Any:
    assert _TEST_DB_URL is not None
    db = await Database.connect(_TEST_DB_URL, min_size=1, max_size=3, statement_cache_size=0)
    tenants = []
    for _ in range(2):
        tid = f"t-{uuid4().hex[:10]}"
        await db.execute("INSERT INTO client_accounts (id, name) VALUES ($1, $1)", tid)
        await db.execute("INSERT INTO tenants (id, name, slug, enabled, client_account_id) "
                         "VALUES ($1, $1, $1, true, $1)", tid)
        tenants.append(tid)
    claims = [AuthClaims(subject="admin", role=Role.CLIENT_ADMIN, tenant_id=t) for t in tenants]
    contacts = [await create_contact(db, c, account_id=None, lead_id=None, name="Mike Ross",
                                     email=f"mike{i}@example.com", phone="+17865550123",
                                     consent={}) for i, c in enumerate(claims)]
    notify = MagicMock()
    monkeypatch.setattr(service, "send_notification", notify)
    yield {"db": db, "a": claims[0], "b": claims[1], "ca": contacts[0], "cb": contacts[1],
           "notify": notify}
    await db.close()


def _picks(choice: str) -> Any:
    """A stand-in for the model: always picks ``choice``, and remembers what it was shown."""
    seen: list[tuple[str, list[str]]] = []

    async def classify(text: str, labels: list[str]) -> str:
        seen.append((text, labels))
        return choice
    classify.seen = seen  # type: ignore[attr-defined]
    return classify


async def _alert_to(db: Database, claims: AuthClaims, emails: list[str]) -> None:
    await call_repo.upsert_call_config(db, claims, auth_token=uuid4().hex, values={
        "enabled": True, "plivo_auth_id": "MAXXXXXXXXXXXXXXXXXX", "from_number": "+17865550000",
        "transfer_number": None, "company_name": "Acme Roofing Marketing",
        "voice": "Polly.Joanna-Neural", "voice_model": None, "voicemail_policy": "hangup",
        "voicemail_message": None, "max_call_seconds": 300, "max_concurrent_calls": 2,
        "recording_enabled": True, "missed_call_alert_emails": emails})


async def _hot_alerts(db: Database, claims: AuthClaims) -> list[Any]:
    return list(await db.fetch(
        "SELECT recipient, subject, body FROM notification_jobs WHERE tenant_id = $1 "
        "AND payload->>'kind' = 'hot_lead_alert' ORDER BY recipient", claims.tenant_id))


async def test_a_client_without_settings_gets_the_roofing_set_and_saves_its_own(
    env: dict[str, Any],
) -> None:
    db, a, b = env["db"], env["a"], env["b"]
    assert await qrepo.get_config(db, a) == DEFAULT_CONFIG
    mine = QualConfig.model_validate({
        **DEFAULT_CONFIG.model_dump(),
        "weights": {"fit": 50, "intent": 20, "urgency": 20, "engagement": 10},
        "questions": [{"key": "area", "ask": "Do you work in Florida?", "category": "fit",
                       "choices": [{"label": "yes", "points": 100},
                                   {"label": "no", "points": 0}]}]})
    await qrepo.save_config(db, a, mine)
    assert await qrepo.get_config(db, a) == mine
    assert await qrepo.get_config(db, b) == DEFAULT_CONFIG  # another client's is untouched


async def test_an_answer_is_mapped_to_a_choice_and_scored(env: dict[str, Any]) -> None:
    db, a, ca = env["db"], env["a"], env["ca"]
    classify = _picks("in 1 to 3 months")
    picked = await service.record_answer(db, a, ca, "start", "probably after the holidays",
                                         channel="call", classify=classify)
    assert picked == "in 1 to 3 months"
    text, labels = classify.seen[0]
    assert text == ("Question: When would you like to get started?\n"
                    "Answer: probably after the holidays")
    assert labels == ["this month", "in 1 to 3 months", "later", "unclear"]
    record = await qrepo.get_record(db, a, ca)
    assert record.answers["start"]["choice"] == "in 1 to 3 months"
    assert record.answers["start"]["heard"] == "probably after the holidays"
    assert record.answers["start"]["channel"] == "call"
    assert (record.score, record.band) == (12, "low")  # urgency 60 x 20%


async def test_an_unclear_answer_or_an_unknown_question_is_not_saved(env: dict[str, Any]) -> None:
    db, a, ca = env["db"], env["a"], env["ca"]
    assert await service.record_answer(db, a, ca, "start", "hmm", channel="call",
                                       classify=_picks("unclear")) is None
    assert await service.record_answer(db, a, ca, "no_such_key", "yes", channel="call",
                                       classify=_picks("yes")) is None
    assert (await qrepo.get_record(db, a, ca)).answers == {}


async def test_engagement_comes_from_what_the_lead_actually_did(env: dict[str, Any]) -> None:
    from api.agents.repository import add_consent

    db, a, ca = env["db"], env["a"], env["ca"]
    await add_consent(db, a, ca, channel="call", status="granted", basis="express_consent",
                      source="widget", wording="w", wording_version="v", evidence={})
    assert await service.rescore(db, a, ca) == 1  # asked for a call: 10 x 10%


async def test_reaching_high_alerts_the_team_once(env: dict[str, Any]) -> None:
    db, a, b, ca, cb = env["db"], env["a"], env["b"], env["ca"], env["cb"]
    await _alert_to(db, a, ["kaustubh@example.com", "sales@example.com"])
    await _alert_to(db, b, ["someone@other.example"])
    for key, choice in _TOP.items():
        await service.record_answer(db, a, ca, key, choice, channel="call",
                                    classify=_picks(choice))
    record = await qrepo.get_record(db, a, ca)
    assert (record.score, record.band) == (90, "high")
    alerts = await _hot_alerts(db, a)
    assert [r["recipient"] for r in alerts] == ["kaustubh@example.com", "sales@example.com"]
    assert alerts[0]["subject"] == "Hot lead: Mike Ross (score 90)"
    body = alerts[0]["body"]
    assert "Phone: +17865550123" in body and "Email: mike0@example.com" in body
    assert "When would you like to get started? this month" in body
    assert env["notify"].delay.call_count == 2

    await service.rescore(db, a, ca)  # still high: no second alert
    assert len(await _hot_alerts(db, a)) == 2
    assert await _hot_alerts(db, b) == []  # nothing for the other client
    assert (await qrepo.get_record(db, b, cb)).answers == {}


async def test_with_nobody_to_tell_the_alert_waits_for_a_list(env: dict[str, Any]) -> None:
    db, a, ca = env["db"], env["a"], env["ca"]
    for key, choice in _TOP.items():
        await service.record_answer(db, a, ca, key, choice, channel="chat",
                                    classify=_picks(choice))
    assert await _hot_alerts(db, a) == []
    await _alert_to(db, a, ["sales@example.com"])
    await service.rescore(db, a, ca)
    assert [r["recipient"] for r in await _hot_alerts(db, a)] == ["sales@example.com"]


async def test_erasing_a_contacts_agent_data_erases_their_answers(env: dict[str, Any]) -> None:
    from api.agents.data_rights import erase_agent_data, export_agent_data

    db, a, ca = env["db"], env["a"], env["ca"]
    await service.record_answer(db, a, ca, "start", "this month", channel="call",
                                classify=_picks("this month"))
    exported = await export_agent_data(db, a, ca)
    assert exported["qualification"]["answers"]["start"]["choice"] == "this month"
    await erase_agent_data(db, a, ca)
    record = await qrepo.get_record(db, a, ca)
    assert (record.answers, record.score) == ({}, None)

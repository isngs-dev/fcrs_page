"""Pure agent-foundation logic (A1): consent rule D5, the calling window across
DST, default cadences D3, the policy validator's invariants, tool-argument
redaction, outbox kind gating and Celery queue routing."""
from __future__ import annotations

import random
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from common.auth import AuthClaims, Role
from common.errors import ValidationError

from api.agents.compliance import consent_allows, in_window, next_window_open
from api.agents.events import emit
from api.agents.models import AgentTask, OutreachPlan
from api.agents.planner import call_first_steps, default_plan
from api.agents.policy import GateInputs, validate
from api.agents.repository import AgentSettings, Consent
from api.agents.tools import redact
from api.config import ApiSettings

_NY = ZoneInfo("America/New_York")
_NOW = datetime(2026, 10, 15, 14, 0, tzinfo=UTC)  # 10:00 in New York
_SETTINGS = AgentSettings(enabled_channels=("call", "email"))


def _consent(channel: str, status: str, basis: str, source: str) -> Consent:
    return Consent(consent_id="c", contact_id="k", channel=channel, status=status, basis=basis,
                   source=source, wording=None, wording_version=None, evidence={},
                   recorded_by=None, recorded_at=_NOW)


# -- D5 -------------------------------------------------------------------------


@pytest.mark.parametrize(("channel", "status", "basis", "source", "allowed"), [
    ("call", "granted", "express_written_consent", "csv", True),
    ("call", "granted", "express_consent", "widget", True),
    ("call", "granted", "express_consent", "email_reply", True),
    ("call", "granted", "express_consent", "call", True),
    ("call", "granted", "express_consent", "csv", False),        # imported "express" isn't enough
    ("call", "granted", "express_consent", "admin_form", False),
    ("call", "granted", "documented_lawful_basis", "admin_form", False),
    ("call", "revoked", "express_written_consent", "csv", False),
    ("email", "granted", "documented_lawful_basis", "admin_form", True),
    ("email", "revoked", "express_consent", "widget", False),
])
def test_consent_rule(channel: str, status: str, basis: str, source: str, allowed: bool) -> None:
    assert consent_allows(channel, _consent(channel, status, basis, source)) is allowed


def test_no_consent_record_blocks_every_channel() -> None:
    assert not consent_allows("email", None)
    assert not consent_allows("call", None)


# -- Calling window (contact-local, DST-safe, end exclusive) --------------------


@pytest.mark.parametrize(("utc", "expected_local_open"), [
    # 2026-03-08: US clocks spring forward. 08:30 EDT -> opens 09:00 EDT (13:00 UTC).
    (datetime(2026, 3, 8, 12, 30, tzinfo=UTC), datetime(2026, 3, 8, 13, 0, tzinfo=UTC)),
    # 2026-11-01: fall back. 08:30 EST -> opens 09:00 EST (14:00 UTC).
    (datetime(2026, 11, 1, 13, 30, tzinfo=UTC), datetime(2026, 11, 1, 14, 0, tzinfo=UTC)),
    # 20:00 local exactly is closed -> next morning 09:00 EST.
    (datetime(2026, 11, 2, 1, 0, tzinfo=UTC), datetime(2026, 11, 2, 14, 0, tzinfo=UTC)),
])
def test_next_window_open_across_dst(utc: datetime, expected_local_open: datetime) -> None:
    assert next_window_open(utc, _NY, _SETTINGS) == expected_local_open


def test_inside_window_is_unchanged() -> None:
    assert next_window_open(_NOW, _NY, _SETTINGS) == _NOW
    assert in_window(_NOW.astimezone(_NY), _SETTINGS)


# -- D3 cadences ----------------------------------------------------------------


def test_default_cadences() -> None:
    inbound = default_plan("inbound_auto", _SETTINGS)
    assert [(s.channel, s.offset) for s in inbound.steps] == [
        ("email", timedelta(minutes=5)),
        ("call", timedelta(days=2, minutes=5)),
        ("email", timedelta(days=4, minutes=5)),
    ]
    outbound = default_plan("outbound_manual", _SETTINGS)
    assert [(s.channel, s.offset) for s in outbound.steps] == [
        ("email", timedelta(0)), ("call", timedelta(days=2)), ("email", timedelta(days=4)),
    ]


# -- Policy validator -------------------------------------------------------------


def _inputs(**over: Any) -> GateInputs:
    base: dict[str, Any] = {"consent_ok": {"email": True, "call": True},
                            "timezone": "America/New_York", "touches": 0, "last_touch_at": None}
    return GateInputs(**{**base, **over})


def _codes(notes: list[Any]) -> list[str]:
    return [n.code for n in notes]


def test_call_without_consent_is_dropped_email_kept() -> None:
    plan, notes = validate(default_plan("outbound_manual", _SETTINGS), _SETTINGS,
                           _inputs(consent_ok={"email": True, "call": False}), _NOW)
    assert plan is not None
    assert [s.channel for s in plan.steps] == ["email", "email"]
    assert "NO_CONSENT" in _codes(notes)


def test_dropped_steps_leave_no_dead_time() -> None:
    """A9: a dropped step gives its slot back -- the kept steps keep the 2-day rhythm
    and the first one starts when the plan starts (gap + window rules still apply)."""
    email_only, _ = validate(default_plan("outbound_csv", _SETTINGS), _SETTINGS,
                             _inputs(consent_ok={"email": True, "call": False}), _NOW)
    assert email_only is not None
    assert [s.offset for s in email_only.steps] == [timedelta(0), timedelta(days=2)]

    calls = AgentSettings(enabled_channels=("call",), call_window_start=time(0, 0),
                          call_window_end=time(23, 59))
    call_only, _ = validate(default_plan("outbound_csv", calls), calls, _inputs(), _NOW)
    assert call_only is not None
    assert [(s.channel, s.offset) for s in call_only.steps] == [("call", timedelta(0))]

    inbound, _ = validate(default_plan("inbound_auto", calls), calls, _inputs(), _NOW)
    assert inbound is not None and inbound.steps[0].offset == timedelta(minutes=5)


def test_call_without_timezone_is_dropped() -> None:
    plan, notes = validate(default_plan("outbound_manual", _SETTINGS), _SETTINGS,
                           _inputs(timezone=None), _NOW)
    assert plan is not None and [s.channel for s in plan.steps] == ["email", "email"]
    assert "CONTACT_TIMEZONE_UNKNOWN" in _codes(notes)


def test_nothing_allowed_returns_none() -> None:
    plan, notes = validate(default_plan("outbound_manual", _SETTINGS),
                           AgentSettings(enabled_channels=()), _inputs(), _NOW)
    assert plan is None and _codes(notes)[-1] == "NO_ALLOWED_CHANNEL"


def test_touch_budget_counts_previous_touches() -> None:
    plan, notes = validate(default_plan("outbound_manual", _SETTINGS), _SETTINGS,
                           _inputs(touches=2, last_touch_at=_NOW - timedelta(days=10)), _NOW)
    assert plan is not None and len(plan.steps) == 1
    assert _codes(notes).count("MAX_TOUCHES") == 2


def test_gap_clamps_after_a_recent_touch_but_inbound_first_email_is_exempt() -> None:
    recent = _inputs(touches=1, last_touch_at=_NOW - timedelta(hours=1))
    outbound, notes = validate(default_plan("outbound_manual", _SETTINGS), _SETTINGS, recent, _NOW)
    assert outbound is not None and outbound.steps[0].offset == timedelta(hours=47)
    assert "GAP_CLAMPED" in _codes(notes)

    inbound, _ = validate(default_plan("inbound_auto", _SETTINGS), _SETTINGS, recent, _NOW)
    assert inbound is not None and inbound.steps[0].offset == timedelta(minutes=5)


def test_a_form_that_asked_for_a_call_is_called_first_and_right_away() -> None:
    form = OutreachPlan(kind="inbound_auto", steps=call_first_steps(_SETTINGS))
    plan, _ = validate(form, _SETTINGS, _inputs(), _NOW)  # 10:00 in New York
    assert plan is not None and [s.channel for s in plan.steps] == ["call", "email", "email"]
    assert plan.steps[0].offset == timedelta(0) and "just filled in" in plan.steps[0].intent
    recent = _inputs(touches=1, last_touch_at=_NOW - timedelta(hours=1))
    plan, _ = validate(form, _SETTINGS, recent, _NOW)  # they just asked: no gap wait
    assert plan is not None and plan.steps[0].offset == timedelta(0)
    night, notes = validate(form, _SETTINGS, _inputs(), _NOW.replace(hour=2))  # 22:00 NY
    assert night is not None and night.steps[0].offset > timedelta(0)  # never at night
    assert "WINDOW_SHIFTED" in _codes(notes)


def test_dispatchers_poll_fast_enough_to_ring_a_form_within_30_seconds() -> None:
    fields = ApiSettings.model_fields
    assert (fields["agent_event_dispatch_interval_seconds"].default
            + fields["agent_task_dispatch_interval_seconds"].default) <= 10


def test_policy_invariants_hold_for_random_plans() -> None:
    """Property check: whatever the proposal, the output never violates gap,
    touches, window or consent."""
    rng = random.Random(1234)  # noqa: S311
    zones = ["America/New_York", "America/Los_Angeles", "America/Chicago", None]
    for _ in range(400):
        settings = AgentSettings(
            enabled_channels=tuple(rng.sample(["email", "call"], rng.randint(0, 2))),
            max_touches_per_contact=rng.randint(1, 5),
            min_hours_between_touches=rng.choice([1, 24, 48, 72]),
            call_window_start=time(rng.randint(7, 10)), call_window_end=time(rng.randint(17, 21)),
        )
        steps = [AgentTask(channel=rng.choice(["email", "call"]),
                           offset=timedelta(hours=rng.randint(0, 200)), goal="follow_up",
                           intent="x") for _ in range(rng.randint(1, 5))]
        steps.sort(key=lambda s: s.offset)
        kind = rng.choice(["inbound_auto", "outbound_manual"])
        last = _NOW - timedelta(hours=rng.randint(1, 100)) if rng.random() < 0.5 else None
        inputs = GateInputs(consent_ok={"email": rng.random() < 0.8, "call": rng.random() < 0.5},
                            timezone=rng.choice(zones), touches=rng.randint(0, 2) if last else 0,
                            last_touch_at=last)
        now = _NOW + timedelta(minutes=rng.randint(0, 1440))
        plan, _ = validate(OutreachPlan(kind=kind, steps=steps), settings, inputs, now)
        if plan is None:
            continue
        assert len(plan.steps) + inputs.touches <= settings.max_touches_per_contact
        previous = inputs.last_touch_at
        gap = timedelta(hours=settings.min_hours_between_touches)
        for i, step in enumerate(plan.steps):
            at = now + step.offset
            assert step.channel in settings.enabled_channels
            assert inputs.consent_ok[step.channel]
            exempt = kind == "inbound_auto" and i == 0  # they just reached out
            if previous is not None and not exempt:
                assert at >= previous + gap
            if step.channel == "call":
                assert inputs.timezone is not None
                assert in_window(at.astimezone(ZoneInfo(inputs.timezone)), settings)
            previous = at


# -- Redaction ---------------------------------------------------------------------


def test_redaction_hides_emails_and_phones() -> None:
    out = redact({"to": "Jane.Doe@Example.com", "notes": ["call (786) 823-3553 now"], "k": 3})
    assert out == {"to": "***@Example.com", "notes": ["call ***53 now"], "k": 3}


# -- Outbox kind gating ---------------------------------------------------------------


class _Conn:
    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def execute(self, query: str, *args: Any) -> str:
        self.calls.append(args)
        return "INSERT 0 1"


_CLAIMS = AuthClaims(subject="u", role=Role.CLIENT_ADMIN, tenant_id="t-1")


@pytest.mark.parametrize(("kind", "code"), [
    ("nonsense", "UNKNOWN_EVENT_KIND"),
])
async def test_emit_rejects_unhandled_kinds(kind: str, code: str) -> None:
    conn = _Conn()
    with pytest.raises(ValidationError) as exc:
        await emit(conn, _CLAIMS, kind, {})  # type: ignore[arg-type]
    assert exc.value.code == code and conn.calls == []


def test_every_known_kind_now_has_a_handler() -> None:
    """A6 enabled the last one (call_completed): nothing can pile up unhandled."""
    from api.agents.events import ENABLED_KINDS, KNOWN_KINDS

    assert ENABLED_KINDS == KNOWN_KINDS


async def test_emit_writes_tenant_from_claims() -> None:
    conn = _Conn()
    await emit(conn, _CLAIMS, "outreach_requested", {"contact_id": "c"})  # type: ignore[arg-type]
    assert conn.calls[0][0] == "t-1"


# -- Queues (D11) ----------------------------------------------------------------------


def test_task_routing_table() -> None:
    from api.tasks.celery_app import celery_app

    router = celery_app.amqp.router
    assert router.route({}, "agents.dispatch_events")["queue"].name == "agents"
    assert router.route({}, "email_agent.send")["queue"].name == "email"
    assert router.route({}, "call_agent.dial")["queue"].name == "calls"
    assert router.route({}, "scheduling.dispatch_due_reminders")["queue"].name == "celery"
    assert "api.agents.tasks" in celery_app.conf.include

# Spec — `agents` module (Rebecca orchestrator / supervisor)

> **Status:** DRAFT for approval. Schema, events and APIs: [`PHASE0_PLAN.md`](PHASE0_PLAN.md) §3–§5.
> Orchestration decision: [`adr/ADR-001-orchestration.md`](adr/ADR-001-orchestration.md).
> Applies CLAUDE.md §3 in full (repo-layer tenant isolation, no silent fallbacks, idempotent tasks,
> correlation IDs, audit).

## 1. Responsibility

Decide **what** happens next for a contact and **when**; never compose content or talk to providers itself.
It consumes events, produces validated `OutreachPlan`s, dispatches due `AgentTask`s to the Email / Calling
agents, and stops plans on stop conditions.

Module layout (`services/api/src/api/agents/`): `models.py` (Pydantic types) · `policy.py` (pure
validation) · `planner.py` · `events.py` (outbox write + consumer) · `dispatcher.py` (claim + route) ·
`tools/` (one file per tool, each calling another module's **repository**) · `repository.py` ·
`routes.py` (admin API) · `tasks.py` (Celery).

## 2. Types

```python
class AgentTask(BaseModel):
    channel: Literal["email", "call", "sms"]
    offset: timedelta            # from plan start
    goal: Literal["follow_up", "answer_questions", "qualify", "book_meeting", "reminder", "re_engage"]
    intent: str                  # free text the composer/call script uses
    max_attempts: int = 1        # call: per-step attempts (no-answer retries)

class OutreachPlan(BaseModel):
    contact_id: str
    campaign_id: str
    steps: list[AgentTask]       # 1..policy.max_touches_per_contact
    lawful_basis: dict[Literal["email", "call", "sms"], ConsentRef]  # resolved, never LLM-supplied
```

## 3. Planning

1. Deterministic **default plan** from `agent_settings` cadences (inbound / outbound) — always available.
2. Optional LLM refinement: the tenant LLM may propose reordering/intent text from conversation summary,
   intent and qualification score. Its output is parsed into `OutreachPlan` (schema-validated).
3. `policy.validate(plan, settings, consents, suppression, existing_touches)` → **policy wins**: drops
   channels without consent/basis, clamps offsets to calling windows and min gap, truncates to max touches.
   Every clamp is recorded in `plan.policy_notes`. If nothing survives → plan not created, event logged
   with code (e.g. `NO_ALLOWED_CHANNEL`).
4. Persist plan + tasks in one transaction; `idempotency_key = sha256(plan_id, step_index)`.

**New-enquiry sequence (plan 1.4, 2026-11-17 — the PDF's day 0/2/4/7, `planner.inbound_steps`):**
day 0 acknowledgment email (goal `acknowledge`; every email already carries the client's booking
link when one is set) — alongside an immediate call when the form or chat asked for one; day 2 a
helpful email; day 4 a second permitted call; day 7 a final email; then nothing more. Each call step
is ONE attempt (no next-day retry): the day-4 call is the retry, and a missed day-0 call's team alert
says when it is due ("Rebecca will call them again on …"). The acknowledgment is a first response:
exempt from the minimum gap at planning and at the gate (`dispatch.gate_purpose`). With a call first
it waits 15 minutes (`ACK_AFTER_CALL`): a call that connects turns it into the thank-you, so the lead
gets one email, not two minutes apart. Events are handled oldest first within a claimed batch
(`events.claim_due_events`) — a chat's email event before its number event. The default touch
budget is 5 so the whole sequence fits; a client's own limits (touches, call attempts, hours between
touches, calling window, consent) still trim or move it — that is how a client tunes it. Outbound
plans are unchanged (email d0, call d2, email d4).

LLM failure in step 2 → use step 1 (this is a documented, explicit fallback: the default plan is policy
data, not fabricated content) and log `plan_llm_refinement_failed`.

## 4. Task state machine

```
PENDING ──schedule──► SCHEDULED ──claim (atomic UPDATE…RETURNING, FOR UPDATE SKIP LOCKED)──► IN_PROGRESS
IN_PROGRESS ─► COMPLETED | FAILED (attempts ≥ max, error code) | ESCALATED (human needed)
any non-terminal ─► CANCELLED (stop condition / pause / kill switch / human takeover)
```

- Claim pattern copied from `scheduling/reminder_repository.py:claim_due_reminders` (system-scoped,
  beat-driven). Claims are cross-tenant; **every subsequent repository call rebuilds `AuthClaims` from the
  claimed row's `tenant_id`**.
- Retries: transient errors → back to `SCHEDULED` with `scheduled_at = now + backoff(attempt) ± jitter`;
  deterministic errors → `FAILED`. Exhausted → `FAILED` + `agent_events(kind=task_dead_lettered)` visible
  in admin.
- Stale `IN_PROGRESS` (> lease) → reaper returns to `SCHEDULED` (calls: only if provider says not active).
- **The ComplianceGate runs inside the claimed task immediately before send/dial**, not only at planning
  time (consent, suppression, window, kill switch can change between planning and execution).

## 5. Stop conditions

`email_reply_received`, `meeting_booked`, `opt_out_received`, `human_takeover`, campaign cancelled,
contact suppressed → in one statement set all the contact's non-terminal tasks to `CANCELLED` with
`result.reason`, and the plan to `stopped`. An in-flight call is not hung up by a stop condition except
opt-out ("don't call me" is handled live by the calling agent).

## 6. Tools (typed, tenant-scoped, audited)

| Tool | Owner module (repository) | Write? |
|---|---|---|
| `search_knowledge(query)` → answer, sources, band | `rag` + orchestrator grounding contract | no |
| `get_contact(contact_id)` | `contacts` | no |
| `get_available_slots(range, tz)` | `scheduling` | no |
| `book_meeting(contact_id, slot)` | `scheduling` | yes |
| `update_lead_stage(lead_id, stage)` | `leads.pipeline` | yes |
| `log_activity(contact_id, kind, payload)` | `timeline` / leads activity | yes |
| `send_email(draft)` | email agent → `notifications` | yes |
| `place_call(task)` | calling agent → telephony | yes |
| `add_to_suppression(channel, value, reason)` | `agents` (compliance) | yes |
| `escalate_to_human(summary, reason)` | notifications + leads activity | yes |

Rules: the runtime injects `AuthClaims`; tools never accept `tenant_id`, and recipients are always the
plan's contact (an LLM can't redirect a send). Each call writes `agent_tool_calls` (args redacted) +
`audit_events` (hash-chained).

## 7. Kill switches

`platform_flags.outreach_paused` (PLATFORM_ADMIN) and `agent_settings.outreach_paused` (CLIENT_ADMIN) are
read **fresh** (no cache) by the gate before every send/dial; pausing does not cancel plans — tasks stay
`SCHEDULED` and resume when unpaused (unless their window expired → re-planned by policy).

## 7b. One person per phone number (plan 1.1, owner decision 2026-11-17)

A lead whose dialable number is already on a contact **is that contact**, even with a different name
or email ("same person"). Matching order in `inbound_leads._ensure_contact`: the contact linked to
the lead → the email's owner → the number's owner → a new contact. On a number match the newest
name and email win, the old email stays an identity (it still finds them), and the contact's
`lead_id` points at the newest lead. The chat makes its contact at the email, before the number:
when the number then belongs to another contact, the chat's contact is folded into it (consents,
identities, memory moved; its plans deleted; the row removed) — only while that contact was made
from this lead and nothing has gone out to it. One with history keeps it (no full merge).
Found in the 2026-11-17 demo: two contacts on one phone meant two sets of call limits.

## 7c. Qualification and the lead score (plans 1.2/1.3, owner decisions 2026-11-17)

Module `api/qualification` (tables `tenant_qualification_configs`, `contact_qualifications`,
migration 0079). Each client sets up to 10 questions in the console (Outreach settings →
Lead qualification); each question counts toward fit, intent or urgency and has 2–6 answer
choices worth 0–100 points. No saved settings = the owner-approved roofing set
(`scoring.DEFAULT_CONFIG`). The score is deterministic (`scoring.score`): each category
averages its questions' points, **an unanswered question counts as zero**, weights default
40/30/20/10 and must add up to 100; engagement (10%) is what the lead did — spoke with Rebecca
40, replied to an email 30, chatted 20, asked for a call 10 (cap 100). Bands: 75+ high, 45–74
medium, under 45 low. The model only maps an answer onto a configured choice
(`service.record_answer`, "unclear" saves nothing); points always come from the settings.

- **Asking:** Rebecca asks at most 2 per call — after they say what they need (then offers the
  booking) or in the one follow-up after a pause (R33) — never during booking; answers are
  mapped after the call (`after_call`), so the live call never waits on a model. The chatbot
  asks at most 2 per conversation, appended to an answered question once name + email (+
  number or the number ask) are in, and only once the agents have made the visitor's contact.
  A question back instead of an answer is answered and the question dropped. Already-answered
  questions (any channel) are never asked again.
- **Rescoring:** after an answer, after every call, after an email reply, and when a lead
  arrives (chat/form). Event handlers use `rescore_safely` (a failure is logged, not retried
  with the event).
- **Hot-lead alert:** the first time a contact reaches high, the missed-call alert list is
  emailed once (`alerted_at`); with nobody on the list the alert waits until there is.
- **Data rights:** export includes the answers and score; erase deletes them. Folding a chat
  contact into another (7b) moves its answers (the target's own answers win).

## 8. Tests (DoD)

Policy: property tests (no output plan ever violates limits). State machine: every legal/illegal
transition. Exactly-once: two concurrent dispatchers, one claim. Isolation: tenant A event never creates
tasks for tenant B; claims rebuilt from row. RBAC: CLIENT_AGENT only own contacts. Stop conditions: each
event cancels remaining steps. Live SQL check for every new table/index.

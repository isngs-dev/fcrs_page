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

## 7d. Booking confirmations and the manage link (plan 1.6, owner decision 2026-11-18)

Every native booking's confirmation (Rebecca on a call, the chat's booking card, the manage page)
carries, when there is something to say:
- **What we'll cover** — their own words, newest last, at most 3 (`memory.agenda`): the form's
  service and message, and what they said they need on a call or in the chat. Other form fields
  (company, …) and corrected details are not topics.
- **To help us prepare** — the first qualification question (§7c) still unanswered.
- **Manage your booking** — a signed link (`scheduling/manage.py`, purpose `booking.v1`, names one
  booking of one tenant; preferred over a Calendly link). Without `EMAIL_LINK_SECRET` +
  `PUBLIC_API_BASE_URL` it is left out and the old "contact us" line stays.

`scheduling/booking.py` is shared: `next_open_slots`, `book_slot` (Rebecca's calls and the page),
`retire_booking` (cancel + skip reminders + delete the calendar event; Rebecca's reschedule and the
page), `send_confirmation` and `confirmation_extras` (the chat route builds its own email but uses
the extras + link; a lookup failure only drops the extras, logged). Calendly bookings keep
Calendly's own link.

**The manage page** (`/public/bookings/{token}`, `scheduling/manage_routes.py`): the token is the
only credential — forged, or a booking not in that tenant → 404. GET shows the booking, up to 12
open times and a cancel button, and changes nothing. *Move* books the new time FIRST, then retires
the old booking (a failed new booking leaves the old one standing), sends the "rescheduled"
confirmation with a new link, tells the rep when the calendar names one, audits
`scheduling.booking_moved`. *Cancel* retires it (the calendar event's removal shows on the rep's
calendar), audits `scheduling.booking_cancelled`, and puts `booking_cancelled_by_attendee` in the
console feed. A booking that is no longer upcoming shows "no longer booked".

## 7e. The chatbot offers a time when the visitor is ready (plan 1.5, 2026-11-18)

`identity_capture.buying_signal` spots a ready visitor from their own words (getting started,
signing up, going ahead, a quote/estimate/proposal, when or how soon we can start, a price for
THEIR business) — no extra model call; ponytail: a phrase list. When such a question is answered
(`intent == "question"`, no other code-owned line that turn), the answer ends with
`BOOKING_OFFER`, once a conversation and never over an upcoming booking. The reply decides: "yes"
opens the booking card filled in from what they already gave (only missing name/email/phone is
asked for, then Step 5.6 opens it); "no" gets `BOOKING_OFFER_DECLINED_REPLY`; a question is just
answered. Exploring visitors (questions about what we do, general pricing, areas) are never pushed.

**Live chat fixes (2026-11-18).** Once our own email ask (`ASK_EMAIL_INTENT`) has gone out, the
model is no longer told it may ask (`EMAIL_GUIDANCE`) — it had asked again two answers later. A
bare "nothing" is a goodbye (`is_closing`); a short "no / no thanks / nope / I'm good" is one only
right after our reply asked "what else / anything else …?" (`asked_what_else`) — a "no" to the
booking offer keeps its own meaning. The goodbye rings a waiting chat call at once, as before.

The turn cap (`turn_cap`, default 6) stays as the fallback to scheduling, per CLAUDE.md §1 and the
`conversation-orchestrator` skill. Raising or removing that default to fully match the PDF's "don't
force scheduling prematurely" is a spec change for the owner (the demo client's cap is 100).

## 7f. After the meeting — no-show recovery (plan 2.1, owner decision 2026-11-18)

Marking a past meeting in the console (`POST /admin/outreach/meetings/{id}:outcome`) now emits
`meeting_no_show` or `meeting_attended` (`agents/meetings.py`). A **no-show** stops the contact's
active plan and starts a `no_show_recovery` plan: a friendly reschedule email now, **one call the
next business day** (Mon–Fri in the contact's zone) **for anyone with call consent** (owner: not
only high-scoring leads), and a final email on day 3 — then nothing more. All the usual gates apply
(consent, calling window, gap, touch budget, opt-outs); booking again or a reply stops it. Marked
twice → one sequence. Marked **attended** afterwards (a correction) stops a running recovery
(`stop_reason = meeting_attended`). A meeting nobody we know booked starts nothing (`NO_CONTACT`).
**Attended (plan 2.2).** Marking a meeting attended stops any other running plan for the contact
(a no-show recovery, …), creates ONE open opportunity (an existing open one is kept, never
duplicated) owned by the lead's agent → else the contact's owner → else the next agent in the
client's round-robin (none when that is off; audited `opportunity.created_after_meeting`), and
starts an `after_meeting` plan with one email (`goal = thank_you`): a thank-you that recaps what
they told us, in their words. It is **always a draft** for a person to review and complete — the
AI wasn't in the meeting — whatever the client's email mode (`email_agent.service`). One per
meeting however often it is marked (`has_plan_since`).

**Nurture (plan 2.3).** The housekeeping sweep (`agents.reap_stale`, every 5 min →
`nurture.sweep_system`) finds new-enquiry and no-show plans with nothing left to send whose last
step closed over a day ago, and marks them `completed` (before this, finished plans stayed
`active`). If the lead has no booking coming up and their urgency answer means later, it starts a
`nurture` plan: a light check-in email at their timeline and a last one 30 days on, then nothing —
emails only, gated as usual at send time. "Later" is read from the points the client gave that
answer: 100 → ready (none); 40–99 → first email in 30 days; under 40 → in 90 days; no answer → none.
Email replies like "contact me in March" keep their own path (`email_agent._contact_later`).

**One status for the whole journey (plan 2.4).** `reporting.STATUSES` (and the console's
`OUTREACH_STATUSES`, same order) gained `nurture`, `no_show`, `attended`, `opportunity`, `won`,
`lost`, `disqualified`; `meeting_booked` now means a meeting still `booked` (a marked one is
`attended` / `no_show`). First match wins: opted_out → not_interested → won → lost (unless a new
deal is open) → disqualified → opportunity (open deal) → meeting_booked (so a no-show who rebooked
is booked) → attended → no_show → human_handoff → nurture (active nurture plan) → invalid_contact
→ qualified → engaged → contacted → new.

**Dashboard numbers (plan 2.5).** `GET /admin/outreach/performance` (and the console's Performance
page) adds, for leads whose first plan started in the period: `qualified` / `qualification_rate`
(scored 45+, medium or high) and `hot_leads` (75+); `handed_off` / `handoff_rate` (human takeover,
an email escalated to the team, or a call transferred); `opportunities` (created in the period) and
`meeting_to_opportunity` (÷ attended meetings); and, for admins only, `costs.per_attended_meeting_micros`
(a "qualified meeting" is one that happened). `by_source` (lead source, `manual` without a lead
record) and `by_campaign` give leads, contacted, meetings, qualified and hand-offs per segment,
biggest first. Agents get everything but costs. Time to first response was already there
(`median_response_minutes`).

Migration 0080 adds the Phase 2 plan kinds (`no_show_recovery`, `after_meeting`, `nurture`) to
`outreach_campaigns.kind` in one step.

## 7g. Knowledge categories, approval and the pricing rule (plan 2.6, owner decisions 2026-11-18)

Migration 0081 gives every `knowledge_docs` row a `category` (company, services, industry,
customer_profile, sales_playbook, case_studies, pricing, competitive, faq, general — default
general) and `approved` (default **true**: a new upload is used at once; owner) plus
`reviewed_at` / `reviewed_by`. Upload takes an optional `category` form field (422
`INVALID_CATEGORY`); `PATCH /admin/ingestion/docs/{id}` (CLIENT_ADMIN; PLATFORM_ADMIN via the
tenant-scoped path) changes the category or switches the document on/off, audited
`knowledge.doc_reviewed`; another tenant's document is 404. The console's Knowledge page has a
category picker on upload and, per document, its category and "Used by the agent / Stop using".

**Retrieval reads only approved documents** — vector and keyword search both query the view
`approved_knowledge_chunks` (chunks joined to approved docs, with the category), so the shared
`common.pgvector` helper is unchanged. Looking up a past answer's sources still reads all chunks.

**Pricing rule.** Strict only once the client has at least one approved Pricing document (before
that, nothing changes; owner). Then a price in a reply — `$200`, `$8,500`, `150 dollars` — must be
stated by a retrieved Pricing chunk (matched by amount; percentages are left to the existing
commitments check). Chat: an unsupported price replaces the reply with an exact-quote offer
(`_QUOTE_REPLY`, decision `escalate`, so the booking/lead card follows). Calls: the sentence is
never spoken and the turn is a miss (her "good question for our team" line).

## 8. Tests (DoD)

Policy: property tests (no output plan ever violates limits). State machine: every legal/illegal
transition. Exactly-once: two concurrent dispatchers, one claim. Isolation: tenant A event never creates
tasks for tenant B; claims rebuilt from row. RBAC: CLIENT_AGENT only own contacts. Stop conditions: each
event cancels remaining steps. Live SQL check for every new table/index.

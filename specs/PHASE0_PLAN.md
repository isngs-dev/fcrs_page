# Rebecca Multi-Agent (Email + Calling, Inbound + Outbound) — Phase 0 Plan

> **Status:** DRAFT for approval · rev 2 (product answers applied, §9) · 2026-10-15 · **no production code written.**
> **Companion specs:** [`agents.md`](agents.md) · [`email_agent.md`](email_agent.md) ·
> [`calling_agent.md`](calling_agent.md) · [`compliance.md`](compliance.md)
> **ADRs:** [`adr/ADR-001-orchestration.md`](adr/ADR-001-orchestration.md) ·
> [`adr/ADR-002-voice-pipeline.md`](adr/ADR-002-voice-pipeline.md) ·
> [`adr/ADR-003-inbound-email.md`](adr/ADR-003-inbound-email.md)
> **Relation to earlier plan:** `dev_plan/AGENTIC_REBECCA_PLAN.md` (local only, git-ignored) covered an
> agentic *chat* agent. This plan supersedes its outbound/follow-up parts (AG-6/AG-7). Its **AG-0 funnel
> instrumentation** is still recommended in parallel — see §9.

Everything below was checked against the code on 2026-10-15; file paths are cited so a reviewer can
verify. Where the brief and the code disagree, the code is the fact and the conflict is listed in §2.

---

## 1. How each new piece plugs into what exists

| New piece | Reuses (existing owner) | Adds |
|---|---|---|
| `agents/` supervisor | Celery app + `_CorrelationTask` (`tasks/celery_app.py`); exactly-once claim SQL pattern (`scheduling/reminder_repository.py:claim_due_reminders`); `AuthClaims` + repo-layer tenant filter | Outbox events table, `OutreachPlan`/`AgentTask` state machine, policy validator, dispatcher beat job, tool registry |
| Tool: `search_knowledge` | `rag/service.py:retrieve_hybrid` + the orchestrator's grounded-answer / `NO_ANSWER_FOUND` contract (`orchestrator/service.py`) | A non-chat entry point that returns answer + sources + confidence band |
| Tools: `get_contact`, `update_lead_stage`, `log_activity` | `contacts/repository.py` (has `lead_id`, `email`, `phone`, `consent` jsonb, `owner_agent_id`) + `contact_identities`, `leads/pipeline.py` state machine, `timeline/` | Normalised `phone`/`email` identities (reuse, no new columns); `contacts.timezone` |
| Tools: `get_available_slots`, `book_meeting` | `scheduling/` (native slots, Google Calendar + Meet, Calendly link, reminders) | Booking on behalf of a contact from email/call (no widget card) |
| `send_email` | `notifications/` providers + idempotent `notification_jobs` + branded templates/HTML part (`notifications/providers.py`, `templates.py`) | `TransactionalEmailProvider` Protocol (SendGrid/SES/Mailgun API), per-tenant from/reply-to/signature/physical address, List-Unsubscribe, Message-ID capture |
| Inbound email | — (nothing exists) | `InboundEmailProvider` Protocol: webhook (first) + IMAP poller (second) — ADR-003 |
| `place_call` / inbound calls | **Plivo** turn loop, signature check + HMAC call tickets (`voice_agent/`) — generalised; Twilio confirmation calls (`calls/`) untouched | `TelephonyProvider` Protocol, **Plivo** impl (turn-based XML first, Audio Streaming if needed — ADR-002 rev 2), AMD, recording, `telephony_numbers`, call sessions/turns |
| Compliance gate | `leads.consent` / `contacts.consent` jsonb (booking consent only today) | `contact_consents` (per channel), `suppression_list`, calling windows, cadence counters, kill switches |
| Encryption of new secrets | `common/crypto.SecretBox` (AES-256-GCM) — same as `tenant_notification_configs` | Per-tenant email-provider keys, IMAP creds, telephony creds, webhook signing secrets |
| Audit | `audit_events` (`migrations/versions/0017_audit_events.py`) | **Hash chain** (it is not tamper-evident today — §2) |
| Admin UI | Next.js RSC + shadcn + role-aware patterns (`apps/admin-web`) | "Outreach / Agents" area (A8) |

---

## 2. Conflicts between the brief and the codebase (decide before A1)

| # | Brief assumes | Code reality | Proposal |
|---|---|---|---|
| C1 | "Follow the existing per-queue routing" | **No queue routing exists** — all tasks share the default queue (`tasks/celery_app.py`; CLAUDE.md §5b says named queues arrive "with SR-1.5", never done) | A1 introduces `task_routes` + queues `agents`, `email`, `calls` (+ `default`); **one** worker consumes all (`-Q default,agents,email,calls`) on the current droplet — RAM, §8 |
| C2 | "There is no platform default" for secrets | **Platform defaults exist and are in production:** `PLATFORM_SMTP_*` (SendGrid, all booking emails), `PLATFORM_TWILIO_*` (confirmation calls), `PLATFORM_PLIVO_*` (Call Us) — `config.py:269-312` | New agent channels: **per-tenant only, no platform default** (as the brief says). Existing booking emails/Call Us keep their platform defaults unless you decide to migrate them (separate decision **Q6**) |
| C3 | "Tamper-evident audit trail" exists | `audit_events` is a plain table — no hash chain | A1 adds `prev_hash`/`row_hash` (SHA-256 chain per tenant) for new agent rows; back-filling old rows is optional |
| C4 | "In line with existing LLM usage/cost reporting" | **No per-tenant cost reporting exists** — only Prometheus counters by provider/model (`llm/metered_provider.py`) | A1 adds `usage_costs` (tenant, kind=llm/asr/tts/telephony/email, units, unit price, cost, ref) written by agents; analytics card in A8 |
| C5 | Twilio is the telephony provider | **Two are live:** Twilio (outbound confirmation calls, SMS) and **Plivo** (widget Call Us, switched from Twilio in commit `61cb233`) | **Resolved: Calling Agent on Plivo** (product decision) — reuses the proven `voice_agent` Plivo loop; Twilio confirmation calls stay as they are |
| C6 | ~1 s voice response latency | Tenants run **`gpt-oss:20b`** (reasoning model). Measured on 2026-10-14: **3.7–4.5 s** per grounded answer even at `reasoning_effort=low` | Voice path needs a fast non-reasoning model with streaming (per-tenant `voice_model`), plus a "one moment…" filler. **1 s is not reachable with the current model** — decision **Q8** |
| C7 | Event bus on Celery/Redis | `notifications/emit.py` is a **best-effort feed** ("never raises") — unsuitable for domain events | Transactional **outbox** table (`agent_events`) written in the same request as the change; a dispatcher claims and routes events exactly once (ADR-001) |
| C8 | Consent at lead capture is per channel | Widget captures **one** booking consent (`scheduling/routes.py` consent gate); lead form consent likewise | A4 adds separate email + call (+ SMS) consent checkboxes with stored wording/version |

---

## 3. Data model (all new tables: `tenant_id text NOT NULL`, composite PK/indices lead with `tenant_id`; Alembic, raw SQL access via repositories)

```text
agent_settings(tenant_id PK, enabled_channels text[], email_mode text CHECK(auto_send|draft_for_review),
  outreach_paused bool, calling_window_start time, calling_window_end time, region_profile text,
  max_touches_per_contact int, max_call_attempts int, min_hours_between_touches int,
  max_concurrent_calls int, max_call_seconds int, voicemail_policy text CHECK(hang_up|leave_message),
  voicemail_message text, transfer_number_e164 text, languages text[], voice_name text, voice_model text,
  recording_enabled bool, physical_address text, updated_at, updated_by)
platform_flags(key PK, value jsonb)                      -- global kill switch (no tenant: platform-only, PLATFORM_ADMIN)

contact_consents(tenant_id, consent_id PK, contact_id, channel CHECK(email|call|sms),
  status CHECK(granted|revoked), basis CHECK(express_consent|express_written_consent|documented_lawful_basis),
  source CHECK(widget|admin_form|csv|command|email_reply|call), wording text, wording_version text,
  evidence jsonb, recorded_by text NULL, recorded_at timestamptz)
  INDEX (tenant_id, contact_id, channel, recorded_at DESC)

suppression_list(tenant_id, suppression_id PK, channel CHECK(email|call|sms|all), value_normalised text,
  reason CHECK(unsubscribe|stop|dnc_request|bounce|complaint|wrong_number|manual|registry), source text,
  created_at)  UNIQUE (tenant_id, channel, value_normalised)

contacts  + timezone text NULL
contact_identities (EXISTS, migration 0040; UNIQUE (tenant_id, identity_type, identity_value)) — reused as the
  email/phone lookup: new identity_type 'phone' (E.164), 'email' values lower-cased (A1 D6)

agent_events(tenant_id, event_id PK, kind text, payload jsonb, status CHECK(pending|processing|done|failed),
  attempts int, available_at, created_at, processed_at)   -- outbox
  INDEX (status, available_at) WHERE status = 'pending'

outreach_campaigns(tenant_id, campaign_id PK, name, kind CHECK(inbound_auto|outbound_manual|outbound_csv),
  goal text, status CHECK(active|paused|completed|cancelled), created_by, created_at)
outreach_plans(tenant_id, plan_id PK, campaign_id, contact_id, status CHECK(active|stopped|completed),
  stop_reason text NULL, plan jsonb /* validated OutreachPlan */, policy_version text, created_at)
outreach_tasks(tenant_id, task_id PK, plan_id, step_index int, channel CHECK(email|call|sms),
  goal text, intent text, scheduled_at, status CHECK(pending|scheduled|in_progress|completed|failed|cancelled|escalated),
  attempts int, max_attempts int, claimed_at NULL, completed_at NULL, result jsonb NULL, last_error text NULL,
  idempotency_key text UNIQUE)
  INDEX (status, scheduled_at) WHERE status IN ('pending','scheduled')  -- due-task hot path

email_threads(tenant_id, thread_id PK, contact_id, plan_id NULL, subject, last_message_at, status)
email_messages(tenant_id, message_id PK, thread_id, direction CHECK(outbound|inbound),
  rfc_message_id text, in_reply_to text NULL, references text[] NULL, provider_message_id text NULL,
  from_addr, to_addr, subject, body_text, body_html NULL, classification text NULL,
  classification_confidence real NULL, status CHECK(draft|approved|sent|delivered|bounced|failed|received),
  task_id NULL, created_at)
  UNIQUE (tenant_id, rfc_message_id); INDEX (tenant_id, in_reply_to)

telephony_numbers(number_e164 PK, tenant_id, provider, capabilities text[], active bool)  -- global lookup → tenant
call_sessions(tenant_id, call_id PK, direction, provider_call_sid UNIQUE, from_e164, to_e164, contact_id NULL,
  task_id NULL, status, started_at, answered_at, ended_at, duration_s, amd_result text NULL, outcome text NULL,
  summary text NULL, sentiment text NULL, recording_ref text NULL, recording_consent bool, ai_disclosed bool,
  transferred_to text NULL, cost_cents int NULL)
call_transcript_turns(tenant_id, call_id, turn_index, speaker CHECK(rebecca|caller|system), text,
  confidence_band text NULL, created_at)  PK (tenant_id, call_id, turn_index)

agent_runs(tenant_id, run_id PK, correlation_id, trigger_event_id NULL, agent CHECK(orchestrator|email|call|chat),
  model, status, started_at, ended_at, tokens_in, tokens_out)
agent_tool_calls(tenant_id, run_id, seq, tool, args_redacted jsonb, result_summary jsonb, ok bool,
  error_code NULL, audit_event_id, created_at)  PK (tenant_id, run_id, seq)

usage_costs(tenant_id, cost_id PK, kind CHECK(llm|asr|tts|telephony|email), units numeric, unit text,
  cost_micros bigint, ref_type, ref_id, created_at)
outreach_command_drafts(tenant_id, draft_id PK, raw_text text, parsed jsonb, status CHECK(pending|confirmed|discarded),
  created_by, created_at)      -- "Talk to Rebecca" parse → confirm
audit_events + prev_hash text NULL, row_hash text NULL     -- C3
```

Tenant-isolation note for the two global lookups: `telephony_numbers` and inbound-email route tokens are
the **only** claims-less reads; they return a `tenant_id` used to build claims, exactly like
`gateway.get_tenant_by_client_key` today.

---

## 4. Events (outbox `agent_events.kind`)

| Event | Emitted by | Orchestrator reaction |
|---|---|---|
| `inbound_lead_created` | leads (widget capture) | Build inbound plan (email ±call) if consent + policy allow |
| `outreach_requested` | admin form / CSV / confirmed command | Build outbound plan |
| `email_reply_received` | inbound email | Classify → act (answer/book/suppress/escalate); **stop** remaining steps |
| `email_bounced` / `email_complained` | provider webhooks | Suppress, stop plan |
| `call_completed` | calling agent | Write outcome; next step or stop |
| `meeting_booked` | scheduling | **Stop** plan, notify team |
| `opt_out_received` | unsubscribe link / "don't call me" / STOP | Suppress channel(s), stop plan |
| `human_takeover` | admin console | Stop plan, mark escalated |

---

## 5. APIs & webhooks

**Admin** (`/admin/outreach/...`; RBAC: PLATFORM_ADMIN all · CLIENT_ADMIN full own tenant · CLIENT_AGENT
create + view where `owner_agent_id`/`assigned_agent_id` = self):
`POST /contacts:outreach` (form) · `POST /imports` (CSV upload) → `GET /imports/{id}` (preview + validation
report) → `POST /imports/{id}:commit` · `POST /commands:parse` → `POST /commands/{draft_id}:confirm` ·
`GET/POST /campaigns`, `POST /campaigns/{id}:pause|resume|cancel` · `GET /plans/{id}`, `POST /tasks/{id}:cancel` ·
`GET /email/threads`, `GET /email/drafts`, `POST /email/drafts/{id}:approve|send`, `PUT /email/drafts/{id}` ·
`GET /calls`, `GET /calls/{id}` (transcript, recording link), `POST /calls/{id}:transfer` ·
`GET/POST/DELETE /suppressions` · `GET/PUT /settings` · `POST /pause-all` (tenant kill switch) ·
`PUT /platform/outreach-pause` (PLATFORM_ADMIN) · `GET /analytics/outreach`.

**Public webhooks** (signature-verified, rate-limited, idempotent on provider event id, tenant from route
or number mapping, correlation id propagated):
`POST /public/telephony/plivo/{tenant_route}/answer` (XML for inbound/outbound answer) ·
`…/status` · `…/amd` · `…/turn/{call_ticket}` (`<GetInput>` speech results) · `…/recording` ·
`POST /public/email/inbound/{route_token}` · `POST /public/email/events/{route_token}` (bounces/complaints) ·
`GET /public/email/unsubscribe/{signed_token}` + `POST` (RFC 8058 one-click).

---

## 6. Sprint backlog & estimates

Estimates assume the existing operating model (spec → implement test-first → review → user manual test).
**Dependencies are strict; each sprint is demo-able and reversible by `agent_settings` / kill switch.**

| Sprint | Scope | Est. | Done when (demo) |
|---|---|---|---|
| **A1 Foundations** | Migrations: `agent_settings`, `platform_flags`, `contact_consents`, `suppression_list`, `contacts` normalisation, `agent_events`, `outreach_*`, `agent_runs/tool_calls`, `usage_costs`, audit hash chain. `ComplianceGate`; kill switches; queue routing (C1); outbox dispatcher (beat); `OutreachPlan` + policy validator; due-task claim; tool registry skeleton (`search_knowledge`, `get_contact`, `log_activity`) | 2 wk | curl: create a plan for a seeded contact → tasks scheduled; gate denies each failure code; kill switch blocks; exactly-once claim test with 2 concurrent dispatchers; isolation + RBAC tests |
| **A2 Email out** | Grounded composer (answer/clarify/escalate bands), `TransactionalEmailProvider` + SMTP adapter, threading headers, unsubscribe link + List-Unsubscribe(-Post), physical address, approval mode, CRM write-back | 1.5 wk | Draft appears in review queue → approve → delivered to Mailpit with correct headers; unsubscribe suppresses |
| **A3 Email in** | Inbound webhook (+ IMAP behind Protocol, A3b if needed), reply → thread match, classifier, actions (answer/offer slots/book/suppress/escalate), bounce/complaint → suppression, stop-on-reply | 1.5 wk | Reply "Can we meet Tue 3pm?" → booked + timeline; "unsubscribe" → suppressed; injected instructions in body change nothing |
| **A4 Inbound automation** | Widget consent checkboxes (email/call/SMS, stored wording), `inbound_lead_created` → plan → email within N min (+call if consented & in window) | 1 wk | Visitor chat → lead → follow-up email in Mailpit referencing their open question |
| **A5 Outbound feeding** | Form, CSV import (validation, de-dupe by `email_lower`/`phone_e164`, formula-injection safe), "Talk to Rebecca" parse → confirm card → campaign; sequences; cancel-on-reply | 1.5 wk | CSV of 20 → preview with 3 rejected rows → commit → plans created; unconfirmed parse never launches |
| **A6 Calls out** | `TelephonyProvider` + **Plivo** turn loop (from `voice_agent`), AI + recording disclosure, recording (start after announcement), grounded turns, booking on call, async AMD/voicemail policy, hard limits, transcript/summary/outcome, cost rows; latency measured → A6b decision | 2–2.5 wk | Test call to a team phone: disclosure → Q&A → booked → confirmation email; voicemail path; duration cap; objection stops recording |
| **A6b (conditional)** | Plivo Audio Streaming + streaming STT/TTS behind the same Protocol, only if A6 p50 turn latency > 2.5 s or no barge-in | 2 wk | Same demo, measured ≈ 1 s turns |
| **A7 Calls in + transfer** | Number → tenant routing, caller-ID → contact match, warm transfer, call-me-later reschedule, "don't call" suppression | 1.5 wk | Call the tenant number → identified → asks for a person → warm transfer |
| **A8 Admin UI** | Outreach/Agents area (feed, campaigns, inbox + drafts, calls + transcript/recording, settings, suppression, analytics), lead "Agent activity" tab | 2 wk | Browser walkthrough per role |
| **A9 Hardening** | E2E (inbound→email→reply→booked; outbound→call→booked), prompt-injection set (emails + transcripts), concurrent-call load test, failure drills, runbook, env docs | 1.5 wk | All drills fail **explicitly** + retry; runbook reviewed |

**Total ≈ 15–17 weeks (+2 if A6b).** A6 outbound calls only reach contacts with a call-consent record
(`compliance.md` §2 — imported lists carry consent evidence; others get email first). Voice-model choice
(Q8) still open.

---

## 7. New env vars / secrets

Platform (env, Pydantic Settings, fail-fast when the feature is enabled):

| Var | Purpose |
|---|---|
| `OUTREACH_ENABLED` | Global feature flag (default `false`) |
| `PUBLIC_API_BASE_URL` | Already exists — used to build webhook/WSS URLs |
| `EMAIL_ROUTE_TOKEN_SECRET` | HMAC key for inbound-email route tokens + unsubscribe tokens |
| `AGENT_DISPATCH_INTERVAL_SECONDS`, `IMAP_POLL_INTERVAL_SECONDS`, `STALE_CALL_REAP_SECONDS` | Beat cadences |
| `CELERY_QUEUES` | Queues the worker consumes (C1) |

Per tenant (DB, encrypted with `SecretBox`, **no platform default** for these channels):
**SendGrid** API key (sending + event webhook verification key); inbound-parse route token (ADR-003);
**Plivo** auth ID + auth token, application id, number(s); transfer number; voice model API key if
different from the tenant's chat LLM. (IMAP credentials only if A3b is ever built.)

DNS the client must configure (per sending domain): SPF include, DKIM CNAMEs, DMARC; MX for the
inbound-reply subdomain (webhook path, ADR-003).

---

## 8. RAM impact on the 1.9 GB droplet (no swap)

**Current containers (no `mem_limit` set anywhere):** postgres, pgbouncer, redis, **embeddings**
(PyTorch CPU + `all-MiniLM-L6-v2`), admin-web (Next.js), **api_1 + api_2**, celery-worker (prefork, default
concurrency = CPU count), celery-beat, nginx. Typical resident sizes for these images put the stack at
**~1.4–1.8 GB already** — this is an estimate; **please run `docker stats --no-stream` and `free -m` on the
server** so A1 starts from measured numbers.

| Addition | Est. RAM | Mitigation |
|---|---|---|
| New queues on a **separate** worker | +250–400 MB | **Don't:** one worker, `-Q default,agents,email,calls`, `--concurrency=2` |
| Plivo turn webhooks (in api, P1) | ~0 beyond normal requests | Concurrency cap per tenant + platform; no audio on our side |
| Plivo Audio Streaming (only if A6b) | +5–30 MB per concurrent call (audio relay to hosted STT/TTS) | Re-measure before A6b; resize first |
| IMAP poller | ~0 (runs in the worker) | Webhook first (ADR-003) |
| Local LLM / LangGraph | — | Not proposed |

**Recommendation:** before A6, either (a) resize to **4 GB** (or add a 2 GB swap file as a stop-gap), or
(b) move `embeddings` to its own small droplet. Running live calls on a box that can OOM-kill Postgres is
the single biggest operational risk in this plan.

---

## 9. Decisions (product owner, 2026-10-15) and what is still open

| # | Question | Answer / status |
|---|---|---|
| Q1 | Countries | **US only** — only the `US` compliance profile is built |
| Q2 | Email provider | **SendGrid** (API for sending + event webhook; Inbound Parse for replies, ADR-003) |
| Q3 | Call languages | **English** |
| Q4 | Call recording | **Yes** — announced in the first sentence, starts after the announcement, stops + deletes on objection (`compliance.md` §5) |
| Q5 | Default cadence | **Follow-ups every 2 days.** Inbound: first email within N min of capture (N=5 default) → call +2 d (if consented, in window) → email +2 d; stop at 3 touches. Outbound: email d0 → call d2 → email d4 |
| — | Calling provider | **Plivo** (ADR-002 rev 2) |
| Q10 | Outbound AI calls to an imported list | **Closed:** import carries per-row consent evidence; contacts without a call-consent record get email first (consent capture), never an AI call — `compliance.md` §2 |
| Q6 | Keep platform defaults for existing booking emails / Call Us? | **Open** — recommendation: keep; new agent channels per-tenant only |
| Q7 | Move Call Us to the Calling Agent's per-tenant Plivo setup later? | **Open** — revisit after A7 |
| Q8 | Fast voice model for calls (C6) | **Open — needed before A6.** `gpt-oss:20b` is too slow for natural calls |
| Q9 | Cost budget per tenant/month | **Open — needed before A6** (cost cap in the gate) |
| Q11 | Droplet resize (§8) | **Open** — please send `docker stats --no-stream` + `free -m`; recommendation: 4 GB before A6 |
| Q12 | Run AG-0 funnel instrumentation in parallel? | **Open** — recommendation: yes |

**A1 can start now** — none of the open items block it (Q8/Q9/Q11 block A6 only).

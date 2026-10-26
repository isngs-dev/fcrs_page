# Runbook — Rebecca's email and calling agents

> For whoever operates the platform (on-call, platform admin, a client admin who asks "why didn't it…?").
> Written in A9 (2026-10-19), extended in A14 (2026-10-21) for the final scope. Specs: [`agents.md`](agents.md),
> [`email_agent.md`](email_agent.md), [`calling_agent.md`](calling_agent.md), [`compliance.md`](compliance.md),
> sprints [`sprints/A1.md`](sprints/A1.md)–[`A14.md`](sprints/A14.md), [`sprints/SCOPE-GAP-PLAN.md`](sprints/SCOPE-GAP-PLAN.md).

The one rule behind everything below: **the agents never fail quietly and never make things up.** Every
failure leaves a status and an UPPER_SNAKE error code on the task / email / call row, a structured log
line, and — where a person is needed — a `agent_*_needs_human` notification in the console.

---

## 1. What runs where

| Process | What it does for the agents |
|---|---|
| `api` (×2) | Admin routes, widget, the public webhooks (SendGrid Inbound Parse + Event Webhook, Plivo call webhooks, lead intake: form webhook, forwarded Meta notifications, Meta Lead Ads) |
| `celery-worker` | **Must consume** `-Q celery,agents,email,calls` — without `agents` nothing is ever sent or dialled |
| `celery-beat` | The schedule below |

| Beat job | Every | Does | If it stops |
|---|---|---|---|
| `agents-dispatch-events` | 15 s (`AGENT_EVENT_DISPATCH_INTERVAL_SECONDS`) | Runs the outbox: plans new leads, stops plans on reply/booking/opt-out, settles calls | Plans aren't created or stopped |
| `agents-dispatch-due-tasks` | 30 s (`AGENT_TASK_DISPATCH_INTERVAL_SECONDS`) | Claims due steps, re-runs the compliance gate, sends the email / places the call | Nothing goes out |
| `agents-reap-stale` | 5 min | Returns steps whose worker died (lease `AGENT_TASK_LEASE_SECONDS`), expires drafts after `EMAIL_DRAFT_TTL_DAYS`, fails calls with no hangup after `CALL_REAP_GRACE_SECONDS` (`CALL_LOST`, retried while attempts remain), **ends campaigns past their end date** (A12) | Stuck `in_progress` steps, drafts never expire, ended campaigns keep going |
| `agents-seal-audit-chain` | 60 s | Hash-chains new audit rows | Audit rows stay unsealed (verify still works on sealed rows) |
| `agents-purge-recordings` | daily | Deletes call recordings older than the client's retention (default 90 days) at Plivo, then unlinks them | Recordings kept longer than promised |

Health: `GET /healthz` (process up), `GET /readyz` (DB + Redis). Call-turn latency:
`GET /admin/outreach/calls/latency` (p50/p95 of Rebecca's turns).

## 2. Turning the agents on for a client

1. **Email sender** — Outreach settings → Email sender: SendGrid API key (write-only), from address on the
   client's authenticated domain, business name, reply domain (e.g. `reply.client.com`).
   Copy the two URLs shown into SendGrid: **Inbound Parse** (MX of the reply domain → SendGrid) and
   **Event Webhook** (signed; paste SendGrid's verification key back into the form).
2. **Calling** — Outreach settings → AI calling: Plivo Auth ID + Auth Token (write-only), the client's Plivo
   number, business name, optional transfer number, recording + retention days. In the Plivo console create an
   **Application** with the shown Answer and Hangup URLs (POST) and attach the number. A number can belong to
   one client only (`NUMBER_IN_USE`). After setup, **regenerate the Plivo token** and paste the new one.
3. **Agent settings** — channels, email mode (start with *draft for review*), calling window, limits, monthly
   cost cap, the business postal address (required for email; CAN-SPAM).
4. **Widget consent** — the chat asks name → email → phone; the phone is call consent only when given right
   after the disclosure. Leads with ticked boxes are followed up when "Follow up new chat leads automatically"
   is on.
5. **Lead intake** (A10) — Outreach → Lead intake shows the URLs and the steps:
   - *Landing-page forms*: point the form tool / Zapier webhook at the **form URL** (keep it private).
   - *Meta notification e-mails*: add a SendGrid Inbound Parse host (e.g. `leads.client.com`, raw MIME on) with
     the shown URL; in the REBECCA Gmail add that address under Forwarding — Gmail's **confirmation code appears in
     the intake queue**, enter it in Gmail — then a filter "from facebookmail.com → forward".
   - *Meta Lead Ads (direct, optional)*: needs the platform's Meta app (`META_APP_SECRET`, `META_VERIFY_TOKEN`) and
     the client's page ID + page access token.
   - Fill in **what the forms say about being contacted** (e-mail permission proof) and — only if the form has a
     call-consent box — its **exact wording**. Without that wording, form leads are never called.
   - Send one real Meta notification and check it lands as a lead; the parser reads `Label: value` lines.
6. **Working hours** (A12) — follow-up e-mails go out 08:00–18:00 in the person's time by default (Outreach
   settings); the first reply to a new inbound lead is exempt.
7. **Smoke test** — add yourself as a contact with call consent, watch Outreach → Calls; send yourself an
   email and reply "Can we meet tomorrow at 3pm?".

Enabling a channel without its setup is refused (`EMAIL_NOT_READY`, `CALL_NOT_READY`) — by design.

## 3. Kill switches

| Switch | Who | How | Effect |
|---|---|---|---|
| Client pause | CLIENT_ADMIN | Outreach settings → **Pause all outreach** (`POST /admin/outreach/pause`, `/resume`) | Every due step is deferred (`OUTREACH_PAUSED_TENANT`), **no attempt used**; resumes exactly where it stopped |
| Platform pause | PLATFORM_ADMIN | `PUT /admin/outreach/platform/pause {"paused": true}` | Same for every client (`OUTREACH_PAUSED_PLATFORM`); audited under `__platform__` |
| Stop one campaign | CLIENT_ADMIN | Outreach → Campaigns → **Stop** | Its active plans stop, steps cancelled |
| Stop one person | admin or their agent | Lead drawer → Agents → **Take over** | Their plan stops at once |
| Stop one channel | CLIENT_ADMIN | Outreach settings → untick **Email follow-up** or **AI calls** | Steps on that channel are deferred (`CHANNEL_DISABLED`), the other channel continues |
| End a campaign on a date | CLIENT_ADMIN | Outreach → Campaigns → **Ends** | Its plans stop after that day (A12) |

Calls already connected finish normally; pausing only stops new ones.

## 4. What to watch

Start with **Outreach → Exceptions** (A13): failed / escalated steps (**Retry** or **Dismiss**), background jobs that
gave up (**Retry**), what the safety checks blocked this week and why, hand-offs and the intake queue. **Lead
status** shows where every person stands; **Performance** shows response time, reply rate, lead→meeting, show rate,
bounce/complaint/opt-out rates, call connection and cost per lead / meeting — mark past meetings attended / no-show
there so the show rate stays honest.

| Signal | Meaning | Action |
|---|---|---|
| log `agent_event_dead_lettered` / `agent_events.status='failed'` | An outbox event failed 5 times (backoff) | Read `last_error`; fix the cause; set it back to `pending` to replay (handlers are idempotent) |
| tasks with `status='failed'` | A step gave up — `last_error` says why (`SENDGRID_400`, `PLIVO_401`, `LLM_ERROR`, `CONTACT_NO_PHONE`, `CALL_LOST`…) | 4xx-type codes are configuration: fix it, the next plan works. `LLM_ERROR` = model was down for every retry |
| emails `status='failed'` | Sending refused (bad key, sender not set up) — never silently "approved" | Fix the sender; re-request outreach for the contact |
| log `call_answer_failed` | The model failed mid-call; the caller heard the team hand-off | Check the model provider; the lead has a needs-human notification |
| log `voice_agent_miss` (reason `unstated commitment`, `reply failed the output guardrails`) | A reply was blocked — a price/promise not in the knowledge base, a prompt leak, "I'm human" | Expected occasionally. Many in a row → add the missing facts to the knowledge base |
| log `call_recording_purge_failed` / `_incomplete` | Plivo refused a recording delete | Retried next day; persistent → check the client's Plivo token |
| notifications `agent_email_needs_human`, `agent_call_needs_human` | Someone asked for a person, an answer wasn't in the knowledge base, a transfer wasn't picked up, a callback came from an unknown caller | The client's team follows up |
| `GET /admin/audit/verify` | Audit hash chain intact? | Any break = tampering or a bad manual edit — investigate |
| Outreach → Lead intake queue | A form / Meta lead had no usable e-mail or phone, came from an untrusted sender, or Gmail needs its forwarding confirmed | Fix the source or contact them by hand; **Resolve** |

## 5. Failure drills (A9) — each is an automated test

| Breaks | What the system does | Operator | Test |
|---|---|---|---|
| SendGrid 5xx / timeout | Step rescheduled with backoff; the same Message-ID is resent (no duplicate) | Nothing unless it persists | `test_email_agent_integration::test_transient_failure_retries_with_the_same_message_id` |
| SendGrid 4xx (bad key) | Email `failed` + step `failed` with the code; not retried | Fix the key | `test_failure_drills_integration::test_sendgrid_rejecting…` |
| Sender misconfigured at approval | Email `failed` with the code (never left "approved") | Fix the sender | `test_email_agent_integration::test_approve_with_a_broken_sender…` |
| Model down while composing | Step retried with backoff (`LLM_ERROR`), nothing invented; fails after max attempts | Check the provider | `test_failure_drills_integration::test_model_outage…` (×2) |
| Plivo down when dialling | Session `failed`, step retried | — | `…::test_plivo_down_retries_then_dials` |
| Plivo refuses (bad token) | Step `failed` (`PLIVO_401`), not retried | Fix the token | `…::test_plivo_refusing…` |
| Model down mid-call | Caller hears "our team will get back to you", call ends, needs-human notification | — | `…::test_model_down_mid_call…` |
| Duplicate webhooks (Plivo hangup, inbound email) | Processed once | — | `…::test_duplicate_hangup…`, `test_email_inbound_integration::test_inbound_is_stored_once…` |
| Worker dies mid-step | Reaper returns it after the lease; it runs once | — | `test_agents_integration::test_reaper_returns_stale_in_progress_tasks` |
| Outbox handler keeps failing | Backoff, then dead-letter after 5 | See §4 | `test_agents_integration::test_event_retry_backoff_then_dead_letter` |
| Many dispatchers at once | Call cap holds exactly; extras deferred without using attempts | — | `test_call_load_integration` |
| Kill switch flipped mid-run | Steps deferred, no attempt used; resume continues | — | `…::test_kill_switch_mid_flight…` |
| Hostile input (email reply / caller) | Even if the model obeys it: no mail to others, no extra plans, no prices/promises/leaks spoken or sent, transfers only to the configured number | — | `test_injection_set_integration` (14 cases) |
| Redis down | Rate limiting falls back to in-memory (explicit) | Restore Redis | `tests/unit/test_rate_limiting.py` |
| The same Meta notification arrives twice | One lead, one plan | — | `test_scope_acceptance_integration::test_14_1…` |
| An e-mail bounces / a number is wrong | Only that channel stops; a permitted other channel continues | — | `test_event_driven_follow_up_integration` |
| Two people ask for the same slot | One booking (also guarded by a database constraint); the other is offered times or a person | — | `test_scope_acceptance_integration::test_14_6…` |

## 6. Data rights and retention (compliance §5–§6)

- **Recordings** are deleted after the client's retention (Outreach settings → "Delete recordings after",
  default 90 days) by the daily job; transcripts stay with the contact. A caller who objects is never recorded
  again on that call and the partial recording is deleted at once.
- **Export a person's agent data** (CLIENT_ADMIN): `GET /admin/outreach/contacts/{contact_id}/agent-data`
  → plans and steps, consents, suppressions, emails, calls with transcripts. Audited.
- **Erase it**: `DELETE /admin/outreach/contacts/{contact_id}/agent-data` → recordings are deleted at Plivo
  **first** (any failure → `502 RECORDING_DELETE_FAILED`, nothing erased, retry), then plans, emails, calls and
  transcripts. **Consents and suppressions are kept** — proof of consent and the do-not-contact block. Audited.
- Opt-outs (email link, "stop calling", bounces, complaints) land on the do-not-contact list automatically.
  "Not interested" (e-mail or call) blocks every channel with reason *Not interested*; an admin can remove it.
- **Lead memory** (what Rebecca knows about a person, A11) is included in the export and erased with the rest.

## 7. Incidents

| Situation | Do |
|---|---|
| A client's Plivo token leaked | Regenerate it in Plivo; paste the new one in Outreach settings → AI calling (blank keeps the old one). Webhooks verify with the new token immediately |
| SendGrid key leaked | Rotate in SendGrid; paste the new key (write-only field) |
| Spam complaints / bounces spike | Pause the client; check the list source (CSV imports carry consent evidence); complaints are suppressed automatically |
| Calls to wrong numbers | Pause; "wrong number" answers are suppressed automatically; check the import |
| Model provider outage | Nothing to do: email steps retry with backoff, calls hand off to the team; fix or switch the client's provider |
| Someone asks "why did Rebecca say X?" | Outreach → Calls → transcript (and recording, while retained); emails show the knowledge sources used |

## 8. Go-live checklist (deploy)

- [ ] Migrations **0066–0076** applied (`alembic upgrade head`)
- [ ] Env: `EMAIL_LINK_SECRET` (≥ 32 chars), `PUBLIC_API_BASE_URL` (https), `SECRET_ENCRYPTION_KEY`; optional
      `META_APP_SECRET` / `META_VERIFY_TOKEN` / `META_GRAPH_VERSION`; defaults in `deploy/.env.example`
- [ ] **Rebuild the API image** (new dependency `phonenumbers`, A10)
- [ ] Recreate `api_1`, `api_2`, `celery-worker` (with `-Q celery,agents,email,calls`), `celery-beat`
- [ ] Rebuild and redeploy the widget (`widget.js`) and the admin console
- [ ] Per client: §2 steps 1–7; regenerate the Plivo token after setup
- [ ] First real calls: read `GET /admin/outreach/calls/latency` — p50 above 2.5 s triggers the A6b decision
      (audio streaming); confirm the warm-transfer whisper on a real line (A7 N9)
- [ ] Platform admin knows the platform pause (§3)

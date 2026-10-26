# Final scope vs. what is built — gap analysis and plan (A10–A14)

> **Status:** DONE 2026-10-21 — A10–A14 built; see each sprint's *As built* and [`A14.md`](A14.md) for the §14
> acceptance map. Approved 2026-10-19: build A10→A14 in order. Decisions: D1 both (notification emails first, then the
> Meta API), D2 Google Workspace (auto-forward filter), D3/D5 as recommended, D4 one follow-up after 2 days
> **Compared:** *Rebecca Agentic AI Sales Assistant — Solution Scope* (final scope, docx) against sprints
> A1–A9 as built ([`A1.md`](A1.md)–[`A9.md`](A9.md), [`../runbook.md`](../runbook.md)). Every "gap" below was
> checked in the code, not assumed.

## 1. Where we stand

Most of the scope is already built. A1–A9 cover:
- the coordinator and its policy guard (consent per channel, suppression, calling hours, contact limits, cost cap, kill switches)
- the shared contact record with deduplication
- the website chatbot's natural name → email → phone capture with per-channel consent
- CSV import with consent evidence
- the email agent (send, read replies, answer from the knowledge base, book, opt-out / bounce / out-of-office)
- the calling agent (outbound and inbound, AI disclosure, recording, booking, warm transfer)
- the admin area (activity, drafts, calls, campaigns, settings, emergency stop)
- the audit chain, recording retention, data export/erase, injection-proof replies, failure drills, load test and runbook

The scope adds **one new lead source** (Meta / landing-page forms), and asks for more **"keep the conversation going" intelligence** and **reporting**.

## 2. Comparison

| Scope requirement | Status | Evidence / what's missing |
|---|---|---|
| §5A Meta Instant Forms + landing-page forms → lead, without manual forwarding (acceptance #1) | **Missing** | No form/Meta intake exists. Only the widget, CSV, single add and "Talk to Rebecca" |
| §5B Website chatbot captures details + channel permission | Built | A4 + chat name→email→phone |
| §5C Imported lists: validate, dedupe, suppression, consent evidence, block calls without it | Built | A5 |
| §3 Coordinator: one lead state, no duplicate outreach, stop on booking/rejection/opt-out | Built | A1 outbox + one active plan per contact |
| §7 Email is event-driven, multi-turn, not a fixed 4-email sequence | **Partly** | Every reply is answered, but **any reply stops the whole plan** (`inbound_service.handle_reply`). After answering a question nothing ever follows up, and a later "no reply" is never chased |
| §4 Structured memory per lead (questions, needs, objections, promises, last action, next step) read by every agent | **Missing** | The email answer sees only the latest reply; the call sees only its own call; the first email sees the chat summary. No cross-channel memory |
| §6 "Interested but not ready / contact me later (date)" → agree the next step | **Missing** | No `contact_later` handling for email (calls have "call me back") |
| §6 "Don't call me / email only" by email → block that channel | **Missing** | Handled only on a call |
| §6 "Not interested" → record the status, block outreach across channels | **Partly** | The plan stops, but a later campaign could contact them again |
| §6 Bounce / invalid number → stop **that** method, continue on another permitted channel | **Missing** | `email_bounced` stops the whole plan, including permitted calls |
| §7 No-response follow-ups: wait times, max attempts, **working hours for email**, **campaign stop date** | **Partly** | Wait times and caps exist; there is no email sending window and no campaign end date |
| §7 Calling: permission-gated, local hours, AI disclosure, retries, shared stop | Built | A6/A7 |
| §8 Calendar: availability, booking, confirmations, reminders | Built | — |
| §8 Calendar: **reschedule / cancel** by email or call | **Missing** | Rebecca can book, but can't move or cancel |
| §10 Pre-action guard, central suppression, AI transparency, no unsupported commitments, audit, human takeover | Built | A1, A3, A6, A9 (now also on calls) |
| §10 Emergency stop: global / client / campaign / **channel** | Built | Channel = untick it in Outreach settings |
| §11 Lead overview by status (new, contacted, engaged, qualified, meeting booked, not interested, opted out, invalid contact, handoff) | **Missing** | No per-lead outreach status |
| §11 Performance (response time, reply rate, lead→meeting, show/no-show, bounce/complaint/opt-out rate, call connection rate, cost per lead/meeting) | **Partly** | Overview counts and monthly cost only |
| §11 Quality & reliability view (low-confidence, blocked actions, failures, duplicate events) — error queue | **Partly** | Needs-human notifications only. No exceptions view with retry |
| §8 Knowledge versioning / approved templates | Deferred | The knowledge base exists. Proposed as later work (§5) |
| §14 E2E tests incl. duplicated form notifications, delayed replies, booking conflicts | **Partly** | A9 covers the rest; these three depend on the new work |

## 3. Plan

Sprints keep the A-series order and rules (spec → test-first → real-DB tests → walkthrough → As built).

| Sprint | Scope | Done when (demo) | Est. |
|---|---|---|---|
| **A10 Form & Meta intake** | (a) **Landing-page form webhook**: per-client signed URL, field mapping, consent fields → consent evidence. (b) **Lead-notification mailbox**: a per-client intake address (SendGrid Inbound Parse); the REBECCA inbox auto-forwards to it; trusted senders; a parser for Meta's notification email. (c) **Meta Lead Ads webhook**, if chosen (§4 D1): verify token + app-secret signature, fetch the lead from the Graph API. All routes are idempotent per source lead id; dedupe by email/phone; a source/campaign rule decides which channels may start; unparseable or incomplete leads go to an **error queue**, never guessed | The same Meta notification forwarded twice → one lead, one plan; a form without call consent never becomes call-eligible; a malformed email lands in the error queue | 1.5–2 wk |
| **A11 Lead memory + richer replies** | A structured per-lead **memory** (asked, needs, objections, what we said, last channel/action, next step), updated after every chat / email / call and read by email answers, follow-ups and calls. New email reply intents: **contact_later** (+ date → reschedule), **channel preference** ("don't call me" → call blocked for them, "email only"), **reschedule/cancel meeting**. **Not interested** → a lasting contact status that blocks new campaigns (an admin can reverse it) | A reply answers in context of an earlier call; "try me next month" reschedules; "don't call, email is fine" blocks calls only | 2 wk |
| **A12 Event-driven follow-up** | A reply no longer ends the conversation. After a non-final reply Rebecca plans **one** no-response follow-up (configurable wait), within the same caps. Bounce / invalid number stops **only that channel**. **Email sending hours** (contact-local) and a **campaign end date**. Booking conflicts are re-checked at confirm time | Question answered → silence → one follow-up after N days → nothing more; bounce → the permitted call still happens | 1.5 wk |
| **A13 Status, reporting, exceptions** | A per-lead outreach status (the scope's 9 states) with filters and counts. Performance metrics: response time, reply rate, lead→meeting, bounce/complaint/opt-out rate, call connection rate, cost per lead and per meeting; show/no-show once a meeting can be marked attended. An **exceptions** screen: failed steps, dead-lettered events, blocked actions, escalations, the intake error queue, with retry/resolve | An admin sees where every lead is, why anything stopped, and can retry a failure | 2 wk |
| **A14 Regression + E2E for the full scope** | Acceptance §14 end to end: duplicated form notifications, delayed replies, booking conflicts, bounce → other channel, contact-later, not-interested blocking; runbook update | All §14 acceptance criteria demonstrated by tests | 1 wk |

**Total ≈ 8–8.5 weeks.** A10 can start as soon as D1/D2 are answered; A11 and A12 don't depend on it.

## 4. Decisions needed (from scope §13; the rest are already decided)

Already decided earlier: US only, English; SendGrid; Plivo; native calendar + our own lead store as the
authority (CRM sync optional); monthly cost cap per client; follow-up spacing 2 days.

| # | Decision | Recommendation |
|---|---|---|
| D1 | Meta leads: parse the **notification emails**, use the **Meta Lead Ads API/webhook**, or **both**? | **Both, email first.** Parsing works today with no Meta app review; add the API when a Meta app and page access are available |
| D2 | Which mailbox is "REBECCA", on which provider (Google Workspace, Microsoft 365, …)? Can it **auto-forward** form notifications to an intake address? | Auto-forward to a SendGrid Inbound Parse address. No mailbox password stored, same webhook pattern as replies |
| D3 | What consent do the Meta / landing forms capture? | Email = permitted by submitting (express request for contact). AI calls only if the form has a call-consent checkbox with disclosure wording, mapped per form |
| D4 | After someone replies and then goes quiet: how many no-response follow-ups? | **One**, after 2 days, within the existing per-contact touch cap |
| D5 | "Not interested" | Block new outreach on all channels (reason `not_interested`), reversible by an admin |

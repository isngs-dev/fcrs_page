# Spec — Compliance Gate & region rules

> **Status:** DRAFT for approval. **Not legal advice** — every row marked ⚖️ needs a decision from the
> client's/iSN's counsel before the related sprint ships. Implemented in A1; enforced before **every**
> email send and **every** dial (inside the claimed task, not only at planning).

## 1. `ComplianceGate.check(claims, contact, channel, now) -> GateResult`

Pure-ish (reads consents, suppression, settings, flags, touch counters via repositories); fails explicitly
with a stable code; never "warns and sends".

| Order | Check | Code |
|---|---|---|
| 1 | Platform kill switch on | `OUTREACH_PAUSED_PLATFORM` |
| 2 | Tenant kill switch on / channel not enabled | `OUTREACH_PAUSED_TENANT` / `CHANNEL_DISABLED` |
| 3 | Value on suppression list (channel or `all`) | `CONTACT_SUPPRESSED` |
| 4 | No valid basis for the channel (latest consent record `granted`, matching basis rule for the region profile) | `NO_CONSENT` / `NO_LAWFUL_BASIS` |
| 5 | Outside contact-local window (contact `timezone`, else number-derived, else **block**) | `OUTSIDE_CALLING_WINDOW` / `CONTACT_TIMEZONE_UNKNOWN` |
| 6 | Cadence: last touch < `min_hours_between_touches`; touches ≥ max; call attempts ≥ max | `CADENCE_LIMIT` / `MAX_TOUCHES` / `MAX_ATTEMPTS` |
| 7 | Concurrency (calls) | `CALL_CONCURRENCY_LIMIT` (retryable) |
| 8 | Region-profile specific (registry scrub missing/expired, quiet days) | `REGISTRY_CHECK_REQUIRED` / `REGION_RULE` |
| 9 | Monthly cost cap reached | `COST_CAP_REACHED` |

Every result (pass or fail) is written to `agent_tool_calls` + audit with the inputs that decided it.

## 2. ⚖️ Outbound AI calling — the decision that gates Sprint A6 outbound

The FCC's **8 Feb 2024 Declaratory Ruling** treats AI-generated voices as "artificial voice" under the
TCPA: calls using them require the called party's **prior express consent**, and telemarketing calls to
wireless numbers require **prior express written consent**; the ruling allows no carve-out for AI that
"acts like a live agent".

Consequence for the brief's §1B ("iSN team feeds a name + phone → Rebecca calls"): **for US numbers, a
team member's note is not consent.** Outbound **email** to team-fed contacts is permissible under
CAN-SPAM with opt-out + identity + address (basis `documented_lawful_basis`).

**Product-owner decision (2026-10-15):** US only; outbound AI calls go ahead on an **imported calling
database**, no legal review planned. **What the system will do** (so imported lists work without the
platform placing AI calls the ruling prohibits — each unlawful call carries $500–$1,500 statutory damages):

1. **CSV import accepts consent evidence per row:** `call_consent` (yes/no), `consent_date`,
   `consent_source` (e.g. "web form acme.com/quote"), `consent_wording` or a wording reference,
   `consent_type` (`express_written` | `express`). Rows with evidence → `contact_consents`
   (`basis=express_written_consent`, `source=csv`, evidence stored, `recorded_by` = importer).
2. **Rows without call consent are imported** and are eligible for **email** outreach (CAN-SPAM) whose
   goal is to get consent for a call ("Would you like a quick call? Reply YES or pick a time") — a YES
   reply or booking-link click records `express` consent (source `email_reply`), after which the Calling
   Agent may call.
3. **The ComplianceGate blocks AI calls to contacts without a `call` consent record** (`NO_CONSENT`).
   This is not configurable per tenant.
4. **National DNC:** telemarketing calls to numbers on the National DNC Registry are prohibited unless
   the person gave express written consent to *this* seller; the import report flags rows needing a DNC
   scrub, and the gate requires a scrub date for non-consented numbers in any human-call workflow.
5. The importer must confirm, per upload, that consent evidence in the file is genuine (checkbox +
   audit row) — the platform records who asserted it.

Decision **Q10: closed** as above.

## 3. Region profiles (`agent_settings.region_profile`, per tenant; rules as data, not code)

**Product decision: US only.** Only the `US` profile is implemented in A1; the India and EU/UK rows stay
as documentation so a later expansion knows what to build. Any other profile value → `REGION_RULE` block.
US calling window default: **09:00–20:00 contact-local** (inside the federal 8 am–9 pm), stricter state
rules applied as data.

| Profile | Email | Calls (AI voice) | Window / frequency | Registry | ⚖️ open points |
|---|---|---|---|---|---|
| **US** | CAN-SPAM: accurate headers, identify ad if promotional, physical address, working opt-out honoured ≤ 10 business days (we: immediately) | TCPA: prior express (written for telemarketing to mobile) consent for AI voice; disclose identity; opt-out mechanism | Federal 8 am–9 pm local; **state rules stricter** (e.g. some states limit hours/days and attempts per 24 h) — table maintained as data | National DNC + internal DNC for telemarketing | State list contents; whether calls are "telemarketing" vs informational per tenant; recording consent (two-party-consent states) |
| **India** | No CAN-SPAM equivalent; DPDP Act 2023 consent + purpose limitation | TRAI TCCCPR 2018: commercial communication via registered headers/series (e.g. 140-series for promotional voice), DLT registration, NCPR/DND scrub | Per TRAI (09:00–21:00 for promotional) | NCPR scrub before promotional calls | Whether AI calls via a foreign provider are permitted; DLT/header registration owner; consent artefact format under DPDP |
| **EU/UK** | GDPR + PECR/ePrivacy: B2C marketing email needs consent or soft opt-in; B2B rules differ by country | Automated calls need prior consent (PECR) | Member-state rules | Country registries (e.g. TPS/CTPS in UK) | Lawful basis per use; DPIA; data transfer (US providers) |

The gate reads the profile; unknown/unsupported profile → `REGION_RULE` (block), never default-allow.

## 4. Consent capture (A4)

Widget/lead form: separate, unticked checkboxes for **email follow-up** and **phone call (AI assistant)**
(+ SMS if enabled), with wording stored verbatim + `wording_version`, timestamp, source, page URL,
visitor id, IP hash. Revocation (unsubscribe, "don't call", STOP) writes a `revoked` record + suppression.

## 5. Recording (product decision: **on**)

Every call announces recording in the first turn, right after the AI disclosure: "This call is recorded
for quality." Recording starts only **after** the announcement (Plivo Record API on the live call). If
the caller objects ("don't record", "no"), recording stops immediately and the partial file is deleted;
the call continues unrecorded. All 11 all-party-consent states (CA, CT, FL, IL, MD, MA, MT, NV, NH, PA,
WA) are covered by announcing on every call regardless of state. Recordings: retention N days
(tenant setting, default 90), access CLIENT_ADMIN/assigned agent only, deleted with the contact.

## 6. Data subject rights

Export + deletion of a contact across agent tables (threads, messages, calls, transcripts, recordings,
consents — **consent and suppression records are retained** as proof/blocklist, minimised). Not built
today for any table; added in A9 (or earlier if a region profile requires it before go-live).

## 7. Tests

One test per gate code; window computation across DST and timezones; unknown timezone blocks; revocation
beats an older grant; suppression normalisation (E.164, lower-cased email, plus-addressing policy);
profile `US` blocks AI call without written consent.

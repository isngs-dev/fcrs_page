# Change — chat asks for the name first, then email, then phone (replaces SR-14's up-front form)

> **Status:** IMPLEMENTED (awaiting review) · 2026-10-15 · requested by the product owner:
> *"Don't ask for gmail at first, just ask their name and use it … as the user goes on chatting then
> ask for gmail. It should feel natural."*

Applies only to tenants with the identity gate on (`tenant_orchestrator_configs.identity_gate_enabled`);
everyone else is unchanged.

| # | Behaviour | Why |
|---|---|---|
| N1 | First message → a fixed, friendly *"Happy to help with that! Before we dive in, what's your name?"* — no form, no email, no LLM cost. Stored as `decision="identity_gate"`, `intent="ask_name"` (so it never spends `turn_cap` budget, SR-14 D10) | Lower friction than a name+email form before any answer |
| N2 | The next message, if it's a name (short, letters, not a dodge like "no thanks"), is saved on the visitor's lead and the **original question is answered**, by name. If it isn't a name, that message is simply answered — the name is **asked once, never nagged** | The old gate was absolute and blocked answers until both fields were given |
| N3 | Replies use the first name naturally now and then (a validated, letters-only first name is the only visitor text that reaches the system prompt) | Personal, and prompt-injection safe |
| N4 | On the visitor's **3rd substantive turn** (`EMAIL_ASK_TURN`), the model ends its answer with one short, natural sentence asking for the best email so the team can follow up. Asked once | "As the user goes on chatting" |
| N5 | An email typed at any point is captured onto the lead. Just an email → fixed thank-you acknowledgement; email + a question → captured and the question answered. A captured follow-up email no longer opens the booking-details flow | The booking flow used to treat any email as booking details |
| N6 | The captured email is email follow-up consent for A4 automation (wording version `chat-2026-10-a`, server-only); lead consent records `chat_name_captured_at` / `chat_email_captured_at` | Keeps the consent trail per channel |
| N7 | The old `IdentityForm` (name+email+consent checkbox) is no longer triggered by the gate; its endpoint `POST /public/chat/identity` and widget component remain (unused by this path) | Extend, don't rewrite; removable later |

## Phone number (added same day: "also ask for number … easy for the calling agent")

| # | Behaviour | Why |
|---|---|---|
| P1 | The number is asked with a **fixed, code-owned disclosure** (`identity_capture.PHONE_ASK`): *"And what's the best number to reach you? If you share it, our team may call you about your enquiry, including with our AI assistant, and you can ask us to stop anytime."* | AI voice = "artificial voice" under the TCPA (FCC Feb 2024) → prior express consent; the disclosure must be exact, so the model never writes it |
| P2 | When: in the email thank-you (when they give an email on its own); otherwise appended to a normal answer from the visitor's 5th turn (`PHONE_ASK_TURN`), once. Never added to an escalation or a guardrail-blocked reply. Streaming sends it as a final delta | Natural order: name → email → number |
| P3 | A number typed **right after** the disclosure is call consent: `contact_consents` row `channel=call`, `basis=express_consent`, `source=widget`, wording = the disclosure (version `chat-2026-10-a`) → passes the A1 gate (D5). A number typed any other time is saved on the lead but is **not** consent (calls stay blocked with `NO_CONSENT`) | Consent only where the disclosure was shown |
| P4 | The widget sends the browser timezone with every chat message; it becomes the contact's timezone for contact-local calling windows | The gate blocks calls without a timezone |
| P5 | If an email-only follow-up plan exists and nothing has been sent yet, the call consent **rebuilds** it (email → call → email); if a touch already went out, the plan is left alone | One plan per contact (A1); don't reset a running sequence |
| P6 | A number captured this way never opens the booking-details flow | Same as the email (N5) |

**Files:** `services/api/src/api/orchestrator/identity_capture.py` (new), `orchestrator/service.py`
(Step 5.5 + prompt personalization; `_IDENTITY_GATE_REPLY`/`_has_captured_identity` removed),
`agents/consent_wording.py` (chat version + call disclosure), `agents/inbound_leads.py` (re-plan),
`leads/repository.py` (`update_lead_contact` optional phone), `orchestrator/routes.py` (timezone).
Widget: `turn.ts` sends `timezone`. An `identity_gate` reply without an action renders as a normal
bot message.
**Tests:** `tests/unit/test_identity_capture.py`, rewritten SR-14 block in
`tests/unit/test_orchestrator_service.py`, `tests/integration/test_chat_identity_flow_integration.py`
(full multi-turn conversation on a real DB), plus a chat-capture case in
`tests/integration/test_inbound_leads_integration.py`.

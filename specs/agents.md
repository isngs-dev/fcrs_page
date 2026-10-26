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

## 8. Tests (DoD)

Policy: property tests (no output plan ever violates limits). State machine: every legal/illegal
transition. Exactly-once: two concurrent dispatchers, one claim. Isolation: tenant A event never creates
tasks for tenant B; claims rebuilt from row. RBAC: CLIENT_AGENT only own contacts. Stop conditions: each
event cancels remaining steps. Live SQL check for every new table/index.

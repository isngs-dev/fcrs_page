# ADR-001 — Orchestration: typed state machine + Celery (not LangGraph)

- **Status:** Proposed · 2026-10-15
- **Context:** Rebecca needs a supervisor that turns events into multi-day outreach plans and dispatches
  steps to email/call agents, exactly once, tenant-isolated, surviving restarts. Stack is locked
  (CLAUDE.md §4): FastAPI, asyncpg (no ORM), Celery + Beat, provider-agnostic `LLMProvider`. The droplet
  has 1.9 GB RAM. Tenants run `gpt-oss:20b` via an OpenAI-compatible endpoint.

## Options

| | A. Typed state machine + Celery + outbox (proposed) | B. LangGraph (+ checkpointer) |
|---|---|---|
| Durability of multi-day plans | Rows in Postgres (`outreach_tasks`), claimed by the existing atomic-UPDATE pattern | Checkpointer tables; long waits still need an external scheduler (Celery/cron) to resume |
| Exactly-once side effects | DB claim + idempotency keys (proven in reminders) | Graph re-execution on resume must be made idempotent by us anyway |
| Tenant isolation | Repository layer, `AuthClaims` per call (house rule) | Must thread claims through graph state; checkpoint tables need tenant scoping we'd write ourselves |
| LLM provider | Existing Protocol (+ tool calling extension) | LangChain chat-model wrappers; Ollama/gpt-oss support via adapters; a second abstraction beside ours |
| Testability | Pure policy + state transitions, unit tests | Graph tests + framework internals |
| Footprint | No new deps | langgraph + langchain-core (+ checkpointer), more RAM per process |
| Where LLM judgement lives | Planner proposal (validated by policy), composer, classifier, call turns — each a bounded call | Same, expressed as graph nodes |

The hard parts here — durable timers over days, exactly-once sends, policy over LLM, tenant isolation —
are database problems this codebase already solves. LangGraph's strengths (in-process agent loops,
branching, human-in-the-loop interrupts) don't remove any of them; the per-turn "agentic" loops (email
reply handling, call turns) are short and bounded enough to write as plain functions with tool calls.

## Decision

**A.** Plain typed state machine: `OutreachPlan`/`AgentTask` (Pydantic) persisted in Postgres; a
transactional outbox (`agent_events`) for domain events; Celery Beat dispatcher claims due work with the
reminder pattern; per-agent bounded tool loops on our `LLMProvider` (adding `generate_with_tools` with
typed tool specs). LangGraph may be reconsidered for a future long-running conversational graph if one
appears; nothing here locks it out.

## Consequences

- `LLMProvider` gains tool calling (OpenAI-compatible + Anthropic) — prerequisite in A1; verify
  `gpt-oss:20b` tool-call reliability (eval in A1) before relying on it.
- Named Celery queues introduced (none exist today).
- No new framework dependency; RAM unchanged.

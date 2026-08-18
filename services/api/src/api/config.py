"""API-specific settings extending the shared platform Settings.

``ApiSettings`` is a drop-in superset of ``common.settings.Settings``. It adds
cookie and token-TTL knobs needed by the auth module. The cached factory
``get_api_settings()`` replaces ``common.get_settings()`` in the API process.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from common.settings import Settings


class ApiSettings(Settings):
    """Settings for the API service -- extends common.Settings."""

    cookie_secure: bool = True
    cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    cookie_name: str = "access_token"
    access_token_ttl_seconds: int = 3600

    # Password reset token TTL (default 30 min).
    password_reset_ttl_seconds: int = 1800

    # Password reset (S9.2): the base URL the reset link is built from --
    # ``{password_reset_url_base}?token={token}``. Non-secret; override per
    # deploy via env. Retires the S3.x auth_reset_token_log dev-only bridge
    # now that a real reset email is enqueued (S9.2 decision 4).
    password_reset_url_base: str = "http://localhost:3000/reset-password"  # noqa: S105

    # Visitor session TTL (default 30 min). Used by the widget admission flow.
    visitor_session_ttl_seconds: int = 1800

    # Rate limiting: widget admission (by IP + client_key).
    widget_session_rate_limit_max: int = 30
    widget_session_rate_limit_window_seconds: int = 60

    # Rate limiting: auth endpoints (login + password-reset request, by IP).
    auth_rate_limit_max: int = 10
    auth_rate_limit_window_seconds: int = 60

    # CORS: preflight cache duration (seconds).
    cors_preflight_max_age: int = 600

    # CORS: origin-allowlist cache TTL (seconds).
    cors_origin_cache_ttl_seconds: int = 300

    # LLM: default max tokens per completion.
    llm_max_tokens: int = 1024

    # LLM: default/example model (per-tenant config overrides this).
    llm_default_model: str = "claude-opus-4-8"

    # LLM: bounded retries (SDK exp-backoff + jitter, transient failures only).
    llm_max_retries: int = 2

    # LLM: per-call timeout in seconds (applied by the SDK).
    llm_timeout_seconds: float = 30.0

    # Celery broker + result backend.
    # Resolution order (per decision 2 in S5.1):
    #   1. CELERY_BROKER_URL / CELERY_RESULT_BACKEND (explicit overrides)
    #   2. REDIS_URL (reuses the existing Redis for rate-limit/blacklist)
    # If neither resolves to a non-None value at startup, celery_app raises (fail-fast).
    celery_broker_url: str | None = None
    celery_result_backend: str | None = None

    # Document ingestion / object storage (S5.2, S3 driver added for
    # multi-service deploys where the API and ingestion worker run as
    # separate processes with independent filesystems -- "local" only works
    # when both share one disk).
    # storage_backend: "local" (filesystem) or "s3" (any S3-wire-compatible
    #   store -- AWS S3, Cloudflare R2, DigitalOcean Spaces, MinIO, Backblaze
    #   B2 -- via storage_s3_endpoint_url). GCS slots in later.
    # storage_local_root: required when storage_backend="local". If unset the
    #   LocalStorageProvider raises at construction time (fail-fast, CLAUDE.md §3).
    # storage_s3_bucket: required when storage_backend="s3" -- S3StorageProvider
    #   raises at construction time if unset (same fail-fast contract).
    # storage_s3_region / storage_s3_endpoint_url: endpoint_url is None for real
    #   AWS S3; set it to point at an S3-compatible provider instead.
    # storage_s3_access_key_id / storage_s3_secret_access_key: optional -- if
    #   unset, boto3 falls back to its own default credential chain (env vars,
    #   shared config file, IAM role). Never hardcode these; env only.
    # ingestion_max_upload_bytes: maximum accepted upload size (default 10 MiB).
    storage_backend: str = "local"
    storage_local_root: str | None = None
    storage_s3_bucket: str | None = None
    storage_s3_region: str | None = None
    storage_s3_endpoint_url: str | None = None
    storage_s3_access_key_id: str | None = None
    storage_s3_secret_access_key: str | None = None
    ingestion_max_upload_bytes: int = 10_485_760

    # Embedding / chunking (S5.3).
    # embedding_dimension: must match the vector(N) column in knowledge_chunks.
    #   Changing this requires a new migration + full re-embed.
    # chunk_max_chars: maximum characters per text chunk before overflow.
    #   2000 (not the original 1000) so a typical FAQ-style heading block
    #   (heading + several question variants + several answer variants, often
    #   1500-1900 chars in practice) fits as ONE chunk via chunker.py's
    #   heading-aware atomic packing, instead of splitting a question from
    #   its own answer across two chunks with no shared vocabulary to anchor
    #   retrieval back to the topic.
    # chunk_overlap_chars: trailing chars of the previous chunk prepended to
    #   the next chunk as sentence-boundary context. Scaled with max_chars to
    #   keep the same ~15% overlap ratio.
    # SR-24: 384 to match the companion embedding container's sentence-
    # transformers/all-MiniLM-L6-v2 model (migration 0051). Was 768.
    embedding_dimension: int = 384
    chunk_max_chars: int = 2000
    chunk_overlap_chars: int = 300

    # Embedding batching (S12.6).
    # embedding_batch_size: max number of texts sent per embeddings.create()
    #   call. Keeps each upstream request small/fast regardless of document
    #   size -- a single oversized request was the root cause of ingestion
    #   timeouts on large documents (see
    #   dev_plan/HANDOFF_embedding_batch_timeout_fix.md). Default 5 per
    #   knowledge_base/AI_CHATBOT_ENGINEERING_HANDBOOK.md §8/§9's 3-5-chunk
    #   recommendation -- conservative for CPU-bound local embedding backends
    #   (e.g. Ollama), fully overridable via env for hosted providers.
    embedding_batch_size: int = 5

    # RAG retrieval (S6.1).
    # rag_default_top_k: used when the caller does not specify k.
    # rag_max_top_k: hard upper bound k is clamped to -- an unbounded/huge k
    #   from the request must not run against the DB.
    rag_default_top_k: int = 5
    rag_max_top_k: int = 20

    # RAG hybrid retrieval (S6.2).
    # rag_hybrid_candidate_k: candidate depth per leg (vector + keyword) before
    #   RRF fusion -- fuse over a wider pool than the final k.
    # rag_rrf_k: the RRF constant (standard default 60) in 1/(rrf_k + rank).
    # rag_fts_language: the Postgres FTS regconfig (bound param, never
    #   string-interpolated); must match the literal used in the migration
    #   0013 GIN index expression for the index to be used.
    # rag_confidence_floor: a vector hit's score must be >= this to count
    #   toward "coverage" in _compute_confidence.
    # rag_conf_w_top / rag_conf_w_margin / rag_conf_w_coverage: weights for the
    #   richer hybrid confidence formula (top similarity + margin + coverage),
    #   default sum to 1.0.
    rag_hybrid_candidate_k: int = 20
    rag_rrf_k: int = 60
    rag_fts_language: str = "english"
    rag_confidence_floor: float = 0.35
    rag_conf_w_top: float = 0.6
    rag_conf_w_margin: float = 0.25
    rag_conf_w_coverage: float = 0.15

    # Observability (S11.3).
    # sentry_dsn: Sentry DSN URL. When unset or empty, Sentry is a no-op.
    #   Comes from env/.env; never hardcode a real DSN.
    # environment: deployment environment label (dev, staging, production).
    sentry_dsn: str | None = None
    environment: str = "dev"

    # Native scheduling / booking (S8.1).
    # schedule_slot_window_days: default window size (days) for GET /public/schedule/slots
    #   when date_from/date_to are not supplied by the caller.
    # schedule_slot_window_max_days: hard cap on the window span -- compute_slots
    #   enforces this itself too, so a caller-supplied window can't run unbounded.
    schedule_slot_window_days: int = 14
    schedule_slot_window_max_days: int = 60

    # Calendar sync (S8.2).
    # calendar_http_timeout_seconds: per-call httpx timeout for CalendarProvider
    #   free-busy/create-event requests (GoogleCalendarProvider).
    calendar_http_timeout_seconds: float = 10.0

    # Google Calendar OAuth (SR-22) -- ONE platform-level OAuth app (a single
    # Google Cloud project), shared across every tenant; each tenant's
    # CLIENT_ADMIN authorizes it individually via
    # PUT /admin/schedule/calendar/google/authorize, producing a per-tenant
    # refresh token (api.scheduling.calendar_config_repository). All three
    # are optional/unset by default -- a deployment with no Google Calendar
    # tenant never needs them; api.scheduling.calendar.calendar_provider_for_async
    # raises a deterministic CalendarConfigError (GOOGLE_OAUTH_NOT_CONFIGURED)
    # if a "google" tenant is hit while these are unset, never a silent
    # no-op or a fabricated calendar sync.
    google_oauth_client_id: str | None = None
    google_oauth_client_secret: str | None = None
    # Must exactly match a redirect URI registered on the Google Cloud OAuth
    # client -- e.g. https://api.<domain>/admin/schedule/calendar/google/callback.
    google_oauth_redirect_uri: str | None = None
    # google_oauth_state_ttl_seconds: how long an issued OAuth `state` token
    # (google_oauth_state.py) stays valid -- long enough for an admin to
    # actually complete Google's consent screen, short enough that a stale,
    # unused state can't be replayed much later.
    google_oauth_state_ttl_seconds: int = 600

    # admin_web_base_url: where the Google OAuth callback (SR-22, a raw
    # browser redirect FROM Google, not an API caller) sends the admin's
    # browser once the connection is stored -- mirrors
    # password_reset_url_base's same "point this at admin-web in each
    # deploy" posture and default dev port.
    admin_web_base_url: str = "http://localhost:3000"

    # Reminder jobs (S8.3).
    # reminder_poll_interval_seconds: the Celery Beat "dispatch-due-reminders"
    #   periodic task's fixed poll interval.
    # reminder_dispatch_batch_size: LIMIT on the atomic claim UPDATE per tick --
    #   only reduces lock contention/tick cost, correctness does not depend on it.
    # reminder_sink: selects the ReminderSink impl (api.scheduling.reminders
    #   .reminder_sink_for). Only "log" (LogReminderSink) exists this sprint;
    #   S9.2 adds the real notification-service-backed sink.
    reminder_poll_interval_seconds: int = 60
    reminder_dispatch_batch_size: int = 100
    reminder_sink: str = "log"

    # Conversation idle-timeout sweep (SR-25). Nothing else in the codebase
    # ever transitions a conversation out of status='active' -- without this,
    # the admin console's "Ended" tab is permanently empty. Mirrors the
    # reminder poll-interval pair above.
    # conversation_idle_timeout_minutes: how long since the last message (or
    #   started_at, for a conversation with none yet) before a conversation
    #   counts as idle and gets closed.
    # conversation_idle_sweep_interval_seconds: the Celery Beat
    #   "close-idle-conversations" periodic task's fixed poll interval --
    #   looser than the reminder poll (5 min vs 60s) since "ended" status
    #   has no downstream time-sensitive side effect the way a reminder does.
    conversation_idle_timeout_minutes: int = 30
    conversation_idle_sweep_interval_seconds: int = 300

    # Notifications (S9.1).
    # notification_smtp_timeout_seconds: the smtplib.SMTP connect/send timeout
    #   used by SmtpEmailProvider. No default provider setting -- provider is
    #   per-tenant, exactly like calendar (a tenant with no config is a
    #   deterministic NOTIFICATION_NOT_CONFIGURED, not a silent fallback).
    notification_smtp_timeout_seconds: float = 10.0

    # Notifications (S9.3).
    # notification_twilio_timeout_seconds: the httpx.AsyncClient timeout used
    #   by TwilioNotificationProvider for SMS/WhatsApp sends. Mirrors
    #   calendar_http_timeout_seconds/notification_smtp_timeout_seconds. No
    #   Account SID / Auth Token / sender setting here -- those are
    #   per-tenant, encrypted (tenant_notification_configs).
    notification_twilio_timeout_seconds: float = 10.0

    # Orchestrator turn pipeline (S10.1).
    # orchestrator_rag_k: retrieval depth (k) passed to retrieve_hybrid for a turn.
    # orchestrator_history_turns: keep_recent passed to get_working_memory --
    #   the windowed tail of recent messages included in the grounded prompt.
    orchestrator_rag_k: int = 5
    orchestrator_history_turns: int = 10

    # Orchestrator 3-way decision defaults (S10.2). Used by
    # get_orchestrator_config when a tenant has no explicit
    # tenant_orchestrator_configs row -- an unconfigured tenant still routes
    # deterministically. escalate_threshold=0.35 preserves the exact numeric
    # boundary of the retired S10.1 orchestrator_confidence_floor amendment
    # (superseded by the richer answer/clarify/escalate decision -- see
    # api.orchestrator.service._decide).
    orchestrator_default_answer_threshold: float = 0.5
    orchestrator_default_escalate_threshold: float = 0.35

    # Orchestrator turn-count cap default (S10.4). Used by
    # get_orchestrator_config when a tenant has no explicit turn_cap (no row,
    # or a row with turn_cap IS NULL) -- the per-conversation visitor-turn
    # cap; the Nth user turn still answers normally, the (N+1)th is
    # redirected to a scheduling CTA / lead form (strict `>`, S10.4 decision
    # 1). Overridable per-tenant via tenant_orchestrator_configs.turn_cap.
    orchestrator_default_turn_cap: int = 6

    # Conversation analytics (S11.2). Used by GET /admin/analytics/overview
    # when from/to are omitted: default look-back window (days) and the hard
    # cap on the window span so a caller-supplied range can't scan unbounded.
    analytics_default_window_days: int = 30
    analytics_max_window_days: int = 366

    # Calendly hosted handoff (SR-6).
    # calendly_webhook_tolerance_seconds: replay-protection window (decision
    #   4c) -- abs(now - t) must be within this many seconds of the webhook
    #   signature's timestamp, or the request is rejected (401
    #   CALENDLY_SIGNATURE_INVALID). Security-driven; stays tight.
    # calendly_handoff_intent_ttl_seconds: how long a pre-handoff email
    #   correlation record (calendly_handoff_intents) stays eligible for the
    #   webhook's find_handoff_visitor lookup (decision 5c). A visitor may
    #   take longer than the default hour on Calendly's own page -- override
    #   per-deploy if so.
    calendly_webhook_tolerance_seconds: int = 300
    calendly_handoff_intent_ttl_seconds: int = 3600

    # Unified customer-360 timeline (SR-9.3).
    # timeline_cache_ttl_seconds: cache-aside TTL for a resolved timeline page
    #   (D8) -- short and tunable without a deploy; degraded responses are
    #   never cached regardless of this value.
    # timeline_messages_per_conversation: per-conversation cap on how many of
    #   a conversation's most-recent messages enter the merged timeline (D11)
    #   -- stops one long chat from monopolizing a page.
    timeline_cache_ttl_seconds: int = 60
    timeline_messages_per_conversation: int = 20

    # Opportunities / deals (SR-9.4).
    # opportunity_default_currency: used by get_opportunity_config when a
    #   tenant has no explicit tenant_opportunity_configs row -- an
    #   unconfigured tenant's new opportunities are stamped this currency
    #   (D7's knowingly-accepted silent default).
    # opportunity_default_prob_*: the four non-terminal-stage platform
    #   default win-probabilities (D3). Terminal stages (closed_won/
    #   closed_lost) are fixed 100/0 and never read from settings --
    #   see opportunities/pipeline.win_probability_for_stage.
    opportunity_default_currency: str = "USD"
    opportunity_default_prob_prospecting: int = 10
    opportunity_default_prob_qualification: int = 25
    opportunity_default_prob_proposal: int = 50
    opportunity_default_prob_negotiation: int = 75

    # Round-robin lead assignment (SR-20 D1). Used by
    # api.leads.assignment_config_repository.get_assignment_config when a
    # tenant has no explicit tenant_assignment_configs row -- an unconfigured
    # tenant is deterministic and OFF, so an un-opted tenant's create_lead
    # behavior is byte-identical to pre-SR-20 (assigned_agent_id stays NULL).
    assignment_round_robin_default: bool = False

    # Workspace settings (SR-20 D5). Used by
    # api.admin.workspace_repository.get_workspace when a tenant has no
    # explicit timezone/language stored -- an unconfigured tenant resolves to
    # these platform defaults, never a guessed/fabricated value written back
    # as if configured (no backfill, no silent fallback).
    workspace_default_timezone: str = "UTC"
    workspace_default_language: str = "en"

    # Notifications feed retention (SR-21 D7). notification_events is the
    # first append-only, write-on-every-lead, never-read-again table in the
    # product; this bounds it. Consumed by the "prune-notification-events"
    # Celery Beat task (api.notifications.events_tasks.prune_notification_events),
    # which deletes rows older than this window once per
    # notification_events_prune_interval_seconds.
    notification_events_retention_days: int = 90

    # notification_events_prune_interval_seconds: the Celery Beat
    # "prune-notification-events" periodic task's fixed poll interval
    # (default: once per day -- retention is measured in days, so a daily
    # sweep is more than frequent enough; mirrors reminder_poll_interval_seconds's
    # pattern for the outbound reminder dispatcher).
    notification_events_prune_interval_seconds: int = 86400

    # Lead email qualification (Leads > Board auto-classification).
    # email_mx_check_timeout_seconds: DNS lookup timeout for
    #   api.leads.mx_check's MX/A record checks. A generous-but-bounded
    #   value -- long enough that a normal resolver round-trip never
    #   spuriously reports "error" (ambiguous, never disqualifying), short
    #   enough that a genuinely dead resolver doesn't stall the task.
    email_mx_check_timeout_seconds: float = 3.0

    # Celery Beat "reclassify-captured-leads" isn't scheduled by default
    # (the backfill runs via services/api/src/api/reclassify_captured_leads.py,
    # an operator-triggered one-off script, same shape as
    # seed_sales_call_scheduling.py) -- no interval setting needed here.


@lru_cache(maxsize=1)
def get_api_settings() -> ApiSettings:
    """Return the process-wide API settings, constructed (and validated) once."""
    return ApiSettings()  # type: ignore[call-arg]  # values come from env/.env

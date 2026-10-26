# ADR-003 — Inbound email: provider webhook first, IMAP second (both behind one Protocol)

- **Status:** Proposed · rev 2 · 2026-10-15 · **provider fixed: SendGrid** (product decision) →
  webhook = SendGrid Inbound Parse; bounces/complaints = SendGrid Event Webhook (signed); IMAP stays a
  documented option only (A3b, not scheduled).
- **Context:** The Email Agent must read replies. Nothing inbound exists today. Outbound mail already goes
  through SendGrid (SMTP relay, platform default). Tenant must be resolved from the route or mailbox
  config, never from message headers. RAM is tight (1.9 GB).

## Options

| | Webhook (e.g. SendGrid Inbound Parse, SES receipt → SNS, Mailgun routes) | IMAP poller (Celery Beat) |
|---|---|---|
| Latency | Seconds (push) | Poll interval (e.g. 60 s) |
| Credentials we hold | A webhook verification secret | Each tenant's mailbox password/app password (high-value secret) |
| Setup for the tenant | MX record on a reply subdomain (e.g. `reply.client.com` → provider) | Existing mailbox; IMAP enabled; app password |
| Thread headers | Raw MIME available | Raw MIME available |
| Load on our box | Request per message | Persistent connections / polling per tenant |
| Failure mode | Provider retries on 5xx | We must track UIDs, handle reconnects, avoid double-processing |
| Works with tenant's real inbox | Only replies to our Reply-To address | Yes (reads their actual inbox) |

## Decision

One `InboundEmailProvider` Protocol (`parse(raw) -> InboundEmail`) with **webhook first (A3)** and
**IMAP second (A3b, only for tenants who must use their own mailbox)**.

- Webhook route: `POST /public/email/inbound/{route_token}`; `route_token` = HMAC-signed, tenant-bound,
  rotatable; verification: provider signature where offered, otherwise the secret route token + HTTP
  basic auth — ⚠️ check in the A3 spike whether SendGrid Inbound Parse offers request signing (its event
  webhook does; inbound parse historically relied on URL secrets) and record the result in the A3 spec.
- Outbound `Reply-To` = `reply+{thread_token}@reply.{tenant domain}` (or a platform reply domain if the
  tenant can't add MX) so replies land on the webhook with the thread pre-identified; header matching
  (`In-Reply-To`/`References`) remains the primary key.
- Idempotency: unique `(tenant_id, rfc_message_id)`; provider retries are no-ops.
- IMAP (if built): UIDVALIDITY + last UID per mailbox, `FETCH` new only, mark processed by our own table
  (never delete or flag the tenant's mail).

## Consequences

- Tenants need a DNS change (MX on a subdomain) for the webhook path — included in onboarding docs.
- Bounces/complaints come from the provider's **event webhook**, not from parsing replies (deterministic
  suppression).

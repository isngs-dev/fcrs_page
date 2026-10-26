/**
 * Lead capture calls to `POST /public/leads` (S14.3 decision 4, scope item 1).
 *
 * `submitLead` performs the one lead-submission attempt and Zod-validates
 * the response at the trust boundary, mirroring `turn.ts`'s `sendTurn` /
 * `session.ts`'s `mintVisitorSession` pattern exactly. Never throws — every
 * failure path (network error, non-2xx error envelope, a response that
 * fails Zod validation, or no held visitor session) returns a typed
 * `LeadError`. The React layer (`LeadForm`) never touches `fetch`/Zod
 * directly. One attempt, no retry loop (S14.6 owns retry/backoff UX).
 */
import { z } from "zod";

import { authHeader } from "./session";
import type { ChannelConsentsPayload } from "./channelConsent";
import type { WidgetConfig } from "./config";

/**
 * Consent copy baked into the bundle for now (S14.3 decision 3) — there is
 * no per-tenant consent-copy source of truth delivered to the widget at
 * runtime today. `LeadForm`'s checkbox label and the submitted `consent`
 * object both reference these constants so what's shown == what's stored.
 */
export const CONSENT_PURPOSE = "lead_followup";
export const CONSENT_TEXT =
  "I agree to be contacted about my enquiry and consent to my details being stored for that purpose.";

const LeadResponseSchema = z.object({
  lead_id: z.string().min(1),
  status: z.string().min(1),
});

export interface Lead {
  leadId: string;
  status: string;
}

/** The typed shape of the backend's central error envelope, mirroring TurnError. */
export interface LeadError {
  readonly type: "LEAD_ERROR";
  /** Backend `error_code` (e.g. CONSENT_REQUIRED, VALIDATION_ERROR) or a local code for network/parse/auth failures. */
  readonly errorCode: string;
  readonly message: string;
  /** Present when the backend returned a well-formed error envelope. */
  readonly correlationId: string | null;
  /** HTTP status, when a response was received at all. */
  readonly status: number | null;
  /**
   * Best-effort `Retry-After` (seconds), parsed from the response when the
   * browser exposes it (S14.6 decision 3). `null` when unreadable/absent —
   * never a fabricated value.
   */
  readonly retryAfterSeconds: number | null;
}

export type LeadResult = { ok: true; lead: Lead } | { ok: false; error: LeadError };

export interface SubmitLeadConsent {
  granted: true;
  purpose: string;
  text: string;
}

export interface SubmitLeadInput {
  name: string;
  email: string;
  phone?: string;
  consent: SubmitLeadConsent;
  /** A4: optional per-channel follow-up consent (only when the boxes were shown). */
  channelConsents?: ChannelConsentsPayload;
  /** A4: the visitor's IANA timezone, for contact-local calling windows. */
  timezone?: string;
}

interface BackendErrorEnvelope {
  error_code?: unknown;
  message?: unknown;
  correlation_id?: unknown;
}

function parseErrorEnvelope(body: unknown): { errorCode: string; message: string; correlationId: string | null } {
  if (body && typeof body === "object") {
    const envelope = body as BackendErrorEnvelope;
    const errorCode = typeof envelope.error_code === "string" ? envelope.error_code : "UNKNOWN_ERROR";
    const message = typeof envelope.message === "string" ? envelope.message : "Lead submission failed.";
    const correlationId = typeof envelope.correlation_id === "string" ? envelope.correlation_id : null;
    return { errorCode, message, correlationId };
  }
  return { errorCode: "UNKNOWN_ERROR", message: "Lead submission failed.", correlationId: null };
}

/** Best-effort `Retry-After` parse (S14.6 decision 3) — see session.ts's twin. */
function parseRetryAfterSeconds(response: Response | null): number | null {
  if (!response) return null;
  const raw = response.headers.get("Retry-After");
  if (!raw) return null;
  const seconds = Number(raw);
  if (!Number.isFinite(seconds) || seconds < 0) return null;
  return seconds;
}

/**
 * Perform the single lead-submission attempt: `POST {apiBase}/public/leads`
 * with `{ name, email, phone?, source: "widget", consent }` — never a
 * `tenant_id` (it is established server-side, only from the signed visitor
 * session carried in the Bearer token).
 *
 * A single attempt, no retry loop (S14.3 decision 4 / S14.6 owns retry
 * UX). Never throws — every failure path returns a typed LeadError. If no
 * visitor session is held (`authHeader()` returns null), returns a typed
 * error and issues no fetch.
 */
export async function submitLead(config: WidgetConfig, input: SubmitLeadInput): Promise<LeadResult> {
  const auth = authHeader();
  if (!auth) {
    return {
      ok: false,
      error: {
        type: "LEAD_ERROR",
        errorCode: "NO_SESSION",
        message: "No visitor session is held; cannot submit the lead form.",
        correlationId: null,
        status: null,
        retryAfterSeconds: null,
      },
    };
  }

  let response: Response;
  try {
    response = await fetch(`${config.apiBase}/public/leads`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...auth },
      credentials: "omit",
      body: JSON.stringify({
        name: input.name,
        email: input.email,
        ...(input.phone ? { phone: input.phone } : {}),
        source: "widget",
        consent: input.consent,
        ...(input.channelConsents ? { channel_consents: input.channelConsents } : {}),
        ...(input.timezone ? { timezone: input.timezone } : {}),
      }),
    });
  } catch (err) {
    return {
      ok: false,
      error: {
        type: "LEAD_ERROR",
        errorCode: "NETWORK_ERROR",
        message: err instanceof Error ? err.message : "Network request failed.",
        correlationId: null,
        status: null,
        retryAfterSeconds: null,
      },
    };
  }

  let body: unknown;
  try {
    body = await response.json();
  } catch {
    body = null;
  }

  if (!response.ok) {
    const { errorCode, message, correlationId } = parseErrorEnvelope(body);
    return {
      ok: false,
      error: {
        type: "LEAD_ERROR",
        errorCode: response.status === 429 ? "RATE_LIMITED" : errorCode,
        message,
        correlationId,
        status: response.status,
        retryAfterSeconds: parseRetryAfterSeconds(response),
      },
    };
  }

  const parsed = LeadResponseSchema.safeParse(body);
  if (!parsed.success) {
    return {
      ok: false,
      error: {
        type: "LEAD_ERROR",
        errorCode: "INVALID_RESPONSE_SHAPE",
        message: "Lead capture response failed validation.",
        correlationId: null,
        status: response.status,
        retryAfterSeconds: null,
      },
    };
  }

  return {
    ok: true,
    lead: {
      leadId: parsed.data.lead_id,
      status: parsed.data.status,
    },
  };
}

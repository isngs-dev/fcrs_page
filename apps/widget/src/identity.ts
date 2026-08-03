/**
 * Conversation-start identity capture calls to `POST /public/chat/identity`
 * (SR-14 D6/D7, scope item 8).
 *
 * `submitIdentity` performs the one identity-submission attempt and
 * Zod-validates the response at the trust boundary, mirroring `lead.ts`'s
 * `submitLead` / `turn.ts`'s `sendTurn` pattern exactly. Never throws —
 * every failure path (network error, non-2xx error envelope, a response
 * that fails Zod validation, or no held visitor session) returns a typed
 * `IdentityError`. The React layer (`IdentityForm`) never touches
 * `fetch`/Zod directly. One attempt, no retry loop (S14.6-style retry/backoff
 * UX is out of scope for this sibling endpoint, matching `submitLead`).
 *
 * This is a SIBLING of `lead.ts` (D4), not a replacement — the existing
 * `<LeadForm>` / `POST /public/leads` / `CONSENT_PURPOSE`/`CONSENT_TEXT`
 * constants are untouched. The consent purpose/copy here is deliberately
 * NEW and distinct (D7): the visitor asking a question did not ask to be
 * contacted, so `lead.ts`'s "I agree to be contacted about my enquiry"
 * wording would over-claim the lawful basis for this capture. This purpose
 * describes only what is actually happening — a name/email is stored so the
 * conversation can be followed up on.
 */
import { z } from "zod";

import { authHeader } from "./session";
import type { WidgetConfig } from "./config";

/** SR-14 D7 -- a NEW consent purpose/copy pair, distinct from lead.ts's
 * CONSENT_PURPOSE/CONSENT_TEXT ("lead_followup"). Never reused verbatim. */
export const CHAT_IDENTITY_CONSENT_PURPOSE = "chat_identification";
export const CHAT_IDENTITY_CONSENT_TEXT =
  "I consent to my name and email being stored so we can follow up on this conversation.";

const IdentityResponseSchema = z.object({
  lead_id: z.string().min(1),
  status: z.string().min(1),
});

export interface IdentityCaptureOutcome {
  leadId: string;
  status: string;
}

/** The typed shape of the backend's central error envelope, mirroring LeadError/TurnError. */
export interface IdentityError {
  readonly type: "IDENTITY_ERROR";
  /** Backend `error_code` (e.g. CONSENT_REQUIRED, IDENTITY_CAPTURE_FAILED) or a local code for network/parse/auth failures. */
  readonly errorCode: string;
  readonly message: string;
  /** Present when the backend returned a well-formed error envelope. */
  readonly correlationId: string | null;
  /** HTTP status, when a response was received at all. */
  readonly status: number | null;
  /**
   * Best-effort `Retry-After` (seconds), parsed from the response when the
   * browser exposes it. `null` when unreadable/absent — never a fabricated
   * value.
   */
  readonly retryAfterSeconds: number | null;
}

export type IdentityResult =
  | { ok: true; identity: IdentityCaptureOutcome }
  | { ok: false; error: IdentityError };

export interface SubmitIdentityConsent {
  granted: true;
  purpose: string;
  text: string;
}

export interface SubmitIdentityInput {
  name: string;
  email: string;
  consent: SubmitIdentityConsent;
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
    const message = typeof envelope.message === "string" ? envelope.message : "Identity submission failed.";
    const correlationId = typeof envelope.correlation_id === "string" ? envelope.correlation_id : null;
    return { errorCode, message, correlationId };
  }
  return { errorCode: "UNKNOWN_ERROR", message: "Identity submission failed.", correlationId: null };
}

/** Best-effort `Retry-After` parse — see lead.ts's/session.ts's twin. */
function parseRetryAfterSeconds(response: Response | null): number | null {
  if (!response) return null;
  const raw = response.headers.get("Retry-After");
  if (!raw) return null;
  const seconds = Number(raw);
  if (!Number.isFinite(seconds) || seconds < 0) return null;
  return seconds;
}

/**
 * Perform the single identity-submission attempt:
 * `POST {apiBase}/public/chat/identity` with `{ name, email, consent }` —
 * never a `tenant_id`/`visitor_id` (established server-side, only from the
 * signed visitor session carried in the Bearer token).
 *
 * Never throws — every failure path returns a typed `IdentityError`. If no
 * visitor session is held (`authHeader()` returns null), returns a typed
 * error and issues no fetch.
 */
export async function submitIdentity(config: WidgetConfig, input: SubmitIdentityInput): Promise<IdentityResult> {
  const auth = authHeader();
  if (!auth) {
    return {
      ok: false,
      error: {
        type: "IDENTITY_ERROR",
        errorCode: "NO_SESSION",
        message: "No visitor session is held; cannot submit your details.",
        correlationId: null,
        status: null,
        retryAfterSeconds: null,
      },
    };
  }

  let response: Response;
  try {
    response = await fetch(`${config.apiBase}/public/chat/identity`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...auth },
      credentials: "omit",
      body: JSON.stringify({
        name: input.name,
        email: input.email,
        consent: input.consent,
      }),
    });
  } catch (err) {
    return {
      ok: false,
      error: {
        type: "IDENTITY_ERROR",
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
        type: "IDENTITY_ERROR",
        errorCode: response.status === 429 ? "RATE_LIMITED" : errorCode,
        message,
        correlationId,
        status: response.status,
        retryAfterSeconds: parseRetryAfterSeconds(response),
      },
    };
  }

  const parsed = IdentityResponseSchema.safeParse(body);
  if (!parsed.success) {
    return {
      ok: false,
      error: {
        type: "IDENTITY_ERROR",
        errorCode: "INVALID_RESPONSE_SHAPE",
        message: "Identity capture response failed validation.",
        correlationId: null,
        status: response.status,
        retryAfterSeconds: null,
      },
    };
  }

  return {
    ok: true,
    identity: {
      leadId: parsed.data.lead_id,
      status: parsed.data.status,
    },
  };
}

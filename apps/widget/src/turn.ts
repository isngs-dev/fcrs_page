/**
 * Turn calls to `POST /public/chat/message` (S14.2 decision 2, scope item 1).
 *
 * `sendTurn` performs the one non-streaming turn request and Zod-validates
 * the response at the trust boundary, mirroring `session.ts`'s
 * `mintVisitorSession` / `AdmissionResult` pattern exactly. Never throws —
 * every failure path (network error, non-2xx error envelope, a response
 * that fails Zod validation, or no held visitor session) returns a typed
 * `TurnError`. The React layer never touches `fetch`/Zod directly (S14.2
 * decision 2 design note) — this keeps the transport swappable for a later
 * streaming sprint without touching the UI.
 */
import { z } from "zod";

import { authHeader } from "./session";
import type { WidgetConfig } from "./config";

const ChatSourceSchema = z.object({
  doc_id: z.string(),
  chunk_id: z.string(),
  score: z.number().nullable(),
  matched_by: z.array(z.string()),
});

const ChatMessageResponseSchema = z.object({
  conversation_id: z.string().min(1),
  message_id: z.string().min(1),
  reply: z.string(),
  decision: z.enum(["answer", "clarify", "escalate", "blocked", "identity_gate"]),
  confidence: z.number().nullable(),
  sources: z.array(ChatSourceSchema),
  action: z.enum(["lead_form", "schedule_cta", "identity_form"]).nullable().optional(),
});

export interface ChatSource {
  docId: string;
  chunkId: string;
  score: number | null;
  matchedBy: string[];
}

export interface Turn {
  conversationId: string;
  messageId: string;
  reply: string;
  decision: "answer" | "clarify" | "escalate" | "blocked" | "identity_gate";
  confidence: number | null;
  sources: ChatSource[];
  action: "lead_form" | "schedule_cta" | "identity_form" | null;
}

/** The typed shape of the backend's central error envelope, mirroring AdmissionError. */
export interface TurnError {
  readonly type: "TURN_ERROR";
  /** Backend `error_code` (e.g. LLM_ERROR, CONVERSATION_NOT_FOUND) or a local code for network/parse/auth failures. */
  readonly errorCode: string;
  readonly message: string;
  /** Present when the backend returned a well-formed error envelope. */
  readonly correlationId: string | null;
  /** HTTP status, when a response was received at all. */
  readonly status: number | null;
  /**
   * Best-effort `Retry-After` (seconds), parsed from the response when the
   * browser exposes it (S14.6 decision 3). `null` when unreadable/absent —
   * never a fabricated value. See `AdmissionError` for the same contract.
   */
  readonly retryAfterSeconds: number | null;
}

export type TurnResult = { ok: true; turn: Turn } | { ok: false; error: TurnError };

export interface SendTurnInput {
  message: string;
  conversationId: string | null;
  /** Optional client-generated UUID for idempotent replay (decision 4 note). Never tenant_id-derived. */
  messageId?: string;
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
    const message = typeof envelope.message === "string" ? envelope.message : "Turn failed.";
    const correlationId = typeof envelope.correlation_id === "string" ? envelope.correlation_id : null;
    return { errorCode, message, correlationId };
  }
  return { errorCode: "UNKNOWN_ERROR", message: "Turn failed.", correlationId: null };
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
 * Perform the single turn attempt: `POST {apiBase}/public/chat/message`
 * with `{ message, conversation_id, message_id? }` — never a `tenant_id`
 * (it is established server-side, only from the signed visitor session
 * carried in the Bearer token).
 *
 * A single attempt, no retry loop (S14.2 decision 7 / S14.6 owns retry
 * UX). Never throws — every failure path returns a typed TurnError. If no
 * visitor session is held (`authHeader()` returns null), returns a typed
 * error and issues no fetch.
 */
export async function sendTurn(config: WidgetConfig, input: SendTurnInput): Promise<TurnResult> {
  const auth = authHeader();
  if (!auth) {
    return {
      ok: false,
      error: {
        type: "TURN_ERROR",
        errorCode: "NO_SESSION",
        message: "No visitor session is held; cannot send a chat message.",
        correlationId: null,
        status: null,
        retryAfterSeconds: null,
      },
    };
  }

  let response: Response;
  try {
    response = await fetch(`${config.apiBase}/public/chat/message`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...auth },
      credentials: "omit",
      body: JSON.stringify({
        message: input.message,
        conversation_id: input.conversationId,
        ...(input.messageId ? { message_id: input.messageId } : {}),
      }),
    });
  } catch (err) {
    return {
      ok: false,
      error: {
        type: "TURN_ERROR",
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
        type: "TURN_ERROR",
        errorCode: response.status === 429 ? "RATE_LIMITED" : errorCode,
        message,
        correlationId,
        status: response.status,
        retryAfterSeconds: parseRetryAfterSeconds(response),
      },
    };
  }

  const parsed = ChatMessageResponseSchema.safeParse(body);
  if (!parsed.success) {
    return {
      ok: false,
      error: {
        type: "TURN_ERROR",
        errorCode: "INVALID_RESPONSE_SHAPE",
        message: "Chat message response failed validation.",
        correlationId: null,
        status: response.status,
        retryAfterSeconds: null,
      },
    };
  }

  const data = parsed.data;
  return {
    ok: true,
    turn: {
      conversationId: data.conversation_id,
      messageId: data.message_id,
      reply: data.reply,
      decision: data.decision,
      confidence: data.confidence,
      sources: data.sources.map((s) => ({
        docId: s.doc_id,
        chunkId: s.chunk_id,
        score: s.score,
        matchedBy: s.matched_by,
      })),
      action: data.action ?? null,
    },
  };
}

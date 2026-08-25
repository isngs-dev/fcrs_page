/**
 * Server-only data layer for the read-only "Conversations" console (design
 * id 4a). Builds the `GET /admin/conversations` query string, calls
 * `adminApiFetch`, and maps the response (or any error) into discriminated
 * result types the page renders directly -- no silent fallbacks (CLAUDE.md
 * §3): a backend error always becomes a visible, honest state, never a
 * blank/faked list or transcript. Mirrors `@/lib/leads`'s exact pattern
 * (`buildLeadsQuery` -> `buildConversationsQuery`, `listLeads` ->
 * `listConversations`, `mapErrorMessage`/`mapDetailErrorMessage`).
 *
 * Constants below are sourced verbatim from the real backend, not invented:
 *  - `CONVERSATION_STATUSES` mirrors `_VALID_STATUSES`
 *    (services/api/src/api/conversation_store/admin_routes.py:38).
 *  - `CONVERSATIONS_PAGE_SIZE` is a UI choice (mirrors S13.4 decision 3's
 *    default of 25), well within the backend's `[1,200]` clamp
 *    (admin_routes.py:137).
 *
 * There is intentionally NO `status="live"`/`"LEAD"` value anywhere here --
 * the real schema only has `active`/`ended` and carries no lead-linkage
 * field (no `lead_id` on `ConversationListItem`/`ConversationDetailResponse`).
 * See `conversations-presentation.ts` for the honest badge mapping and the
 * scope decisions this file's callers must follow.
 */
import "server-only";

import { adminApiFetch, AdminApiError } from "@/lib/api";

/** The two canonical conversation statuses (admin_routes.py `_VALID_STATUSES`). */
export const CONVERSATION_STATUSES = ["active", "ended"] as const;

export type ConversationStatus = (typeof CONVERSATION_STATUSES)[number];

/** Fixed page size for the console's Prev/Next pagination (mirrors leads). */
export const CONVERSATIONS_PAGE_SIZE = 25;

/** A single row of `GET /admin/conversations` -- mirrors `ConversationListItem`
 * (admin_routes.py:41-52) exactly. No `tenant_id` -- the backend response is
 * already leak-free by construction. */
export interface ConversationListItem {
  conversationId: string;
  status: string;
  channel: string;
  visitorId: string | null;
  startedAt: string;
  endedAt: string | null;
  messageCount: number;
  summary: string | null;
}

interface ConversationListItemResponseBody {
  conversation_id: string;
  status: string;
  channel: string;
  visitor_id: string | null;
  started_at: string;
  ended_at: string | null;
  message_count: number;
  summary: string | null;
}

interface ConversationListResponseBody {
  items: ConversationListItemResponseBody[];
  total: number;
  limit: number;
  offset: number;
}

export type ConversationsResult =
  | { status: "ok"; items: ConversationListItem[]; total: number; limit: number; offset: number }
  | { status: "error"; message: string; correlationId: string };

export interface ConversationsQueryParams {
  page: number;
  status?: string;
}

/**
 * Pure, unit-testable query builder mirroring `buildLeadsQuery`. Clamps
 * `page >= 1`, derives `offset = (page-1) * CONVERSATIONS_PAGE_SIZE`, and
 * drops any `status` value not in `CONVERSATION_STATUSES` (unknown/blank ->
 * no status filter at all, so a stray/hand-edited `?status=live` degrades to
 * "no filter" rather than a hard 422 on first paint -- the 422 mapping in
 * `listConversations` below is the belt-and-suspenders for the rare case a
 * bad value still reaches the backend).
 */
export function buildConversationsQuery(params: ConversationsQueryParams): string {
  const page = Number.isFinite(params.page) && params.page >= 1 ? Math.floor(params.page) : 1;
  const offset = (page - 1) * CONVERSATIONS_PAGE_SIZE;

  const query = new URLSearchParams();
  query.set("limit", String(CONVERSATIONS_PAGE_SIZE));
  query.set("offset", String(offset));

  const status = params.status?.trim();
  if (status && (CONVERSATION_STATUSES as readonly string[]).includes(status)) {
    query.set("status", status);
  }

  return query.toString();
}

function toConversationListItem(body: ConversationListItemResponseBody): ConversationListItem {
  return {
    conversationId: body.conversation_id,
    status: body.status,
    channel: body.channel,
    visitorId: body.visitor_id,
    startedAt: body.started_at,
    endedAt: body.ended_at,
    messageCount: body.message_count,
    summary: body.summary,
  };
}

/**
 * Fetch a page of the caller's tenant conversations. Never sends a
 * `tenant_id` -- scoping is entirely the backend's repository-layer job from
 * the caller's own claims (CLAUDE.md §3). Never logs the response body
 * (PII-minimal).
 *
 * `tenantId`: when provided, targets the PLATFORM_ADMIN super-user surface
 * `GET /admin/tenants/{tenantId}/conversations` instead of the implicit
 * `GET /admin/conversations` -- same response shape, only the URL prefix
 * differs. Always the route segment's `{tenantId}`, never client state.
 */
export async function listConversations(
  params: ConversationsQueryParams,
  tenantId?: string
): Promise<ConversationsResult> {
  const query = buildConversationsQuery(params);
  const basePath = tenantId
    ? `/admin/tenants/${encodeURIComponent(tenantId)}/conversations`
    : "/admin/conversations";

  try {
    const response = await adminApiFetch(`${basePath}?${query}`);
    const body = (await response.json()) as ConversationListResponseBody;
    return {
      status: "ok",
      items: body.items.map(toConversationListItem),
      total: body.total,
      limit: body.limit,
      offset: body.offset,
    };
  } catch (error) {
    if (error instanceof AdminApiError) {
      return { status: "error", message: mapErrorMessage(error), correlationId: error.correlationId };
    }
    // A network throw (not an AdminApiError) -- the request never reached
    // (or never returned from) admin-api.
    return {
      status: "error",
      message: "Unable to reach the server. Please try again.",
      correlationId: "",
    };
  }
}

function mapErrorMessage(error: AdminApiError): string {
  if (error.status === 403 || error.errorCode === "ROLE_NOT_PERMITTED") {
    return "You do not have permission to view conversations.";
  }
  if (error.status === 401) {
    return "Your session has expired. Please log in again.";
  }
  if (
    error.errorCode === "INVALID_CONVERSATION_FILTER" ||
    error.errorCode === "INVALID_LIST_WINDOW"
  ) {
    return "That filter isn't valid -- showing all conversations.";
  }
  return `Something went wrong (${error.errorCode || "UNKNOWN_ERROR"}). Correlation ID: ${
    error.correlationId || "n/a"
  }.`;
}

function mapDetailErrorMessage(error: AdminApiError): string {
  if (error.status === 404 || error.errorCode === "CONVERSATION_NOT_FOUND") {
    return "This conversation could not be found.";
  }
  if (error.status === 403 || error.errorCode === "ROLE_NOT_PERMITTED") {
    return "You do not have permission to view this conversation.";
  }
  if (error.status === 401) {
    return "Your session has expired. Please log in again.";
  }
  return `Something went wrong (${error.errorCode || "UNKNOWN_ERROR"}). Correlation ID: ${
    error.correlationId || "n/a"
  }.`;
}

// ---------------------------------------------------------------------------
// Conversation transcript pane (4a) -- GET /admin/conversations/{id}. Mirrors
// `admin_routes.py`'s leak-free response shape exactly (no `tenant_id`, no
// lead-linkage field, no source/citation field on messages -- only
// `intent`/`confidence`).
// ---------------------------------------------------------------------------

/** A single transcript message -- mirrors `MessageResponse`
 * (admin_routes.py:66-75) exactly. `sourceCount` (SR-2) is a cheap hint --
 * NOT the resolved sources payload -- so the transcript pane knows which bot
 * messages get a "View sources" affordance. */
export interface ConversationMessage {
  messageId: string;
  role: string;
  content: string;
  intent: string | null;
  confidence: number | null;
  tokens: number | null;
  createdAt: string;
  sourceCount: number;
}

interface MessageResponseBody {
  message_id: string;
  role: string;
  content: string;
  intent: string | null;
  confidence: number | null;
  tokens: number | null;
  created_at: string;
  source_count: number;
}

export interface ConversationDetail {
  conversationId: string;
  status: string;
  channel: string;
  startedAt: string;
  endedAt: string | null;
  summary: string | null;
  messages: ConversationMessage[];
}

interface ConversationDetailResponseBody {
  conversation_id: string;
  status: string;
  channel: string;
  started_at: string;
  ended_at: string | null;
  summary: string | null;
  messages: MessageResponseBody[];
}

export type ConversationDetailResult =
  | { status: "ok"; conversation: ConversationDetail }
  | { status: "error"; message: string; correlationId: string };

function toConversationMessage(body: MessageResponseBody): ConversationMessage {
  return {
    messageId: body.message_id,
    role: body.role,
    content: body.content,
    intent: body.intent,
    confidence: body.confidence,
    tokens: body.tokens,
    createdAt: body.created_at,
    sourceCount: body.source_count,
  };
}

function toConversationDetail(body: ConversationDetailResponseBody): ConversationDetail {
  return {
    conversationId: body.conversation_id,
    status: body.status,
    channel: body.channel,
    startedAt: body.started_at,
    endedAt: body.ended_at,
    summary: body.summary,
    messages: body.messages.map(toConversationMessage),
  };
}

/** Fetch a single conversation's full transcript for the transcript pane.
 * Mirrors `listConversations`'s tenant-scoped-path convention exactly. */
export async function getConversationDetail(
  conversationId: string,
  tenantId?: string
): Promise<ConversationDetailResult> {
  const basePath = tenantId
    ? `/admin/tenants/${encodeURIComponent(tenantId)}/conversations`
    : "/admin/conversations";

  try {
    const response = await adminApiFetch(`${basePath}/${encodeURIComponent(conversationId)}`);
    const body = (await response.json()) as ConversationDetailResponseBody;
    return { status: "ok", conversation: toConversationDetail(body) };
  } catch (error) {
    if (error instanceof AdminApiError) {
      return { status: "error", message: mapDetailErrorMessage(error), correlationId: error.correlationId };
    }
    return {
      status: "error",
      message: "Unable to reach the server. Please try again.",
      correlationId: "",
    };
  }
}

// ---------------------------------------------------------------------------
// Grounding spot-check (SR-2) -- GET .../messages/{message_id}/sources.
// Resolves a bot message's stored `sources` (doc_id/chunk_id/score/
// matched_by) to the real, live knowledge_chunks.content so a reviewer can
// read the reply next to what it was supposedly grounded in and judge
// groundedness for themselves -- no scoring/diff/verdict added here
// (decision 6). Mirrors `getConversationDetail`'s tenant-scoped-path +
// `adminApiFetch` + discriminated-result pattern exactly.
// ---------------------------------------------------------------------------

/** A single resolved citation -- mirrors `MessageSourceItem`
 * (admin_routes.py) exactly. `content` is `null` and `resolved` is `false`
 * when the chunk no longer resolves (deleted/re-ingested, or -- by
 * construction -- a cross-tenant id) -- never dropped, never faked. */
export interface MessageSourceItem {
  chunkId: string;
  docId: string;
  score: number | null;
  matchedBy: string[];
  content: string | null;
  resolved: boolean;
}

interface MessageSourceItemResponseBody {
  chunk_id: string;
  doc_id: string;
  score: number | null;
  matched_by: string[];
  content: string | null;
  resolved: boolean;
}

/** Mirrors `MessageSourcesResponse` (admin_routes.py) exactly -- leak-free
 * (no `tenant_id`). */
export interface MessageSourcesDetail {
  messageId: string;
  content: string;
  decision: string | null;
  confidence: number | null;
  grounded: boolean | null;
  sources: MessageSourceItem[];
}

interface MessageSourcesResponseBody {
  message_id: string;
  content: string;
  decision: string | null;
  confidence: number | null;
  grounded: boolean | null;
  sources: MessageSourceItemResponseBody[];
}

export type MessageSourcesResult =
  | { status: "ok"; detail: MessageSourcesDetail }
  | { status: "error"; message: string; correlationId: string };

function toMessageSourceItem(body: MessageSourceItemResponseBody): MessageSourceItem {
  return {
    chunkId: body.chunk_id,
    docId: body.doc_id,
    score: body.score,
    matchedBy: body.matched_by,
    content: body.content,
    resolved: body.resolved,
  };
}

function toMessageSourcesDetail(body: MessageSourcesResponseBody): MessageSourcesDetail {
  return {
    messageId: body.message_id,
    content: body.content,
    decision: body.decision,
    confidence: body.confidence,
    grounded: body.grounded,
    sources: body.sources.map(toMessageSourceItem),
  };
}

function mapMessageSourcesErrorMessage(error: AdminApiError): string {
  if (error.status === 404 || error.errorCode === "MESSAGE_NOT_FOUND") {
    return "This message could not be found.";
  }
  if (error.status === 403 || error.errorCode === "ROLE_NOT_PERMITTED") {
    return "You do not have permission to view this message's sources.";
  }
  if (error.status === 401) {
    return "Your session has expired. Please log in again.";
  }
  return `Something went wrong (${error.errorCode || "UNKNOWN_ERROR"}). Correlation ID: ${
    error.correlationId || "n/a"
  }.`;
}

/**
 * Fetch a bot message's resolved sources for the grounding spot-check
 * affordance. Mirrors `getConversationDetail`'s tenant-scoped-path
 * convention exactly. Never sends `tenant_id` -- scoping is entirely the
 * backend's repository-layer job from the caller's own claims. Never logs
 * the response body (PII-minimal -- reply text + chunk text never logged).
 */
export async function getMessageSources(
  conversationId: string,
  messageId: string,
  tenantId?: string
): Promise<MessageSourcesResult> {
  const basePath = tenantId
    ? `/admin/tenants/${encodeURIComponent(tenantId)}/conversations`
    : "/admin/conversations";

  try {
    const response = await adminApiFetch(
      `${basePath}/${encodeURIComponent(conversationId)}/messages/${encodeURIComponent(messageId)}/sources`
    );
    const body = (await response.json()) as MessageSourcesResponseBody;
    return { status: "ok", detail: toMessageSourcesDetail(body) };
  } catch (error) {
    if (error instanceof AdminApiError) {
      return {
        status: "error",
        message: mapMessageSourcesErrorMessage(error),
        correlationId: error.correlationId,
      };
    }
    return {
      status: "error",
      message: "Unable to reach the server. Please try again.",
      correlationId: "",
    };
  }
}


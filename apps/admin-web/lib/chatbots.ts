/**
 * Server-only data layer for the CLIENT_ADMIN/CLIENT_AGENT "My chatbots" hub
 * + switcher (multi-chatbot accounts). Mirrors `lib/clients.ts`'s shape:
 * typed helpers calling `adminApiFetch`, mapping the response (or any
 * error) into a discriminated result the pages render directly -- no silent
 * fallbacks (CLAUDE.md §3).
 *
 * Backed by `GET/POST /admin/tenants/mine`
 * (services/api/src/api/admin/routes.py) -- the caller's own
 * `client_account_id`, never a caller-supplied one.
 */
import "server-only";

import { adminApiFetch, AdminApiError } from "@/lib/api";

export interface ChatbotSummary {
  id: string;
  name: string;
  slug: string;
  enabled: boolean;
}

interface OwnTenantRow {
  id: string;
  name: string;
  slug: string;
  enabled: boolean;
}

function toChatbotSummary(row: OwnTenantRow): ChatbotSummary {
  return { id: row.id, name: row.name, slug: row.slug, enabled: row.enabled };
}

export type ChatbotsResult =
  | { status: "ok"; items: ChatbotSummary[] }
  | { status: "error"; message: string; correlationId: string };

/** List every chatbot in the caller's own account (CLIENT_ADMIN or
 * CLIENT_AGENT -- the switcher is symmetric). An empty list is rendered
 * honestly, never fabricated. */
export async function listMyChatbots(): Promise<ChatbotsResult> {
  try {
    const response = await adminApiFetch("/admin/tenants/mine");
    const body = (await response.json()) as { tenants: OwnTenantRow[] };
    return { status: "ok", items: body.tenants.map(toChatbotSummary) };
  } catch (error) {
    if (error instanceof AdminApiError) {
      return { status: "error", message: mapErrorMessage(error), correlationId: error.correlationId };
    }
    return {
      status: "error",
      message: "Unable to reach the server. Please try again.",
      correlationId: "",
    };
  }
}

export interface CreateChatbotInput {
  name: string;
  slug: string;
}

export interface CreateChatbotResponseBody {
  tenant_id: string;
  name: string;
  slug: string;
  client_key: string;
}

/**
 * Call `POST /admin/tenants/mine` (CLIENT_ADMIN-only). Returns the raw
 * response body -- the one-time `client_key` passes straight through to the
 * caller and must never be logged here (secrets hygiene, matching
 * `lib/clients.ts#onboardClient`). Throws `AdminApiError` on any non-2xx;
 * callers map it to field/form errors themselves.
 */
export async function createMyChatbot(input: CreateChatbotInput): Promise<CreateChatbotResponseBody> {
  const response = await adminApiFetch("/admin/tenants/mine", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: input.name, slug: input.slug }),
  });
  return (await response.json()) as CreateChatbotResponseBody;
}

function mapErrorMessage(error: AdminApiError): string {
  if (error.status === 403 || error.errorCode === "ROLE_NOT_PERMITTED") {
    return "You do not have permission to view chatbots.";
  }
  if (error.status === 401) {
    return "Your session has expired. Please log in again.";
  }
  return `Something went wrong (${error.errorCode || "UNKNOWN_ERROR"}). Correlation ID: ${
    error.correlationId || "n/a"
  }.`;
}

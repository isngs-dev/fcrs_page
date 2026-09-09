/**
 * Server-only data layer for the PLATFORM_ADMIN "Clients" tile list + the
 * per-client context header (S13.7). Mirrors `lib/leads.ts`/`lib/settings.ts`'s
 * shape: typed helpers calling `adminApiFetch`, mapping the response (or any
 * error) into a discriminated result the pages render directly -- no silent
 * fallbacks (CLAUDE.md §3): a backend error always becomes a visible, honest
 * state, never a fabricated client row.
 *
 * Tenant-list source (Investigation / Open Question 1): the shipped backend
 * has NO `GET /admin/tenants` list route -- only `POST /admin/tenants`
 * (onboard) and `POST /admin/tenants/{id}/rotate-key`
 * (services/api/src/api/admin/routes.py). The only real list/read endpoints
 * are `GET /debug/tenants` and `GET /debug/tenants/{id}`
 * (services/api/src/api/tenants/routes.py), which run `get_current_claims`
 * (any authenticated role) then `TenantRepository.list`/`get`
 * (services/api/src/api/tenants/repository.py) -- for a global
 * (PLATFORM_ADMIN) caller, `tenant_filter` applies NO tenant restriction, so
 * `list` returns every tenant; a non-global caller sees only their own row.
 * This sprint wraps those two debug routes as the tile view's real,
 * already-wired list/read source, per the Investigation's locked default
 * ("if only single-tenant reads exist, this sprint's scope includes a
 * minimal platform-admin tenant-list call"). Flagged for the user: promoting
 * `/debug/tenants` to a first-class `/admin/tenants` GET (PLATFORM_ADMIN-only,
 * paginated) is a reasonable follow-up but out of scope here (no
 * `services/**` changes this sprint).
 */
import "server-only";

import { adminApiFetch, AdminApiError } from "@/lib/api";

/** A single tenant row as the `tenants` table stores it (tenants/repository.py). */
export interface ClientSummary {
  tenantId: string;
  name: string;
  slug: string;
  enabled: boolean;
  /** Multi-chatbot accounts (migration 0060): the account this chatbot
   *  belongs to. Always present -- every tenant has one. */
  clientAccountId: string;
}

interface DebugTenantRow {
  id: string;
  name: string;
  slug: string;
  enabled: boolean;
  client_account_id: string;
  [key: string]: unknown;
}

function toClientSummary(row: DebugTenantRow): ClientSummary {
  return {
    tenantId: row.id,
    name: row.name,
    slug: row.slug,
    enabled: row.enabled,
    clientAccountId: row.client_account_id,
  };
}

/** A single `client_accounts` row (tenants/repository.py
 *  `ClientAccountRepository`) -- the grouping entity above `tenants`. */
export interface ClientAccountSummary {
  id: string;
  name: string;
}

interface DebugClientAccountRow {
  id: string;
  name: string;
  [key: string]: unknown;
}

export type ClientAccountsResult =
  | { status: "ok"; items: ClientAccountSummary[] }
  | { status: "error"; message: string; correlationId: string };

/**
 * List every client account (PLATFORM_ADMIN-only, `GET /debug/client-
 * accounts`). Used purely to GROUP the existing flat `/clients` tenant list
 * by account -- no other consumer exists yet.
 */
export async function listClientAccounts(): Promise<ClientAccountsResult> {
  try {
    const response = await adminApiFetch("/debug/client-accounts");
    const body = (await response.json()) as DebugClientAccountRow[];
    return { status: "ok", items: body.map((row) => ({ id: row.id, name: row.name })) };
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

export type ClientsResult =
  | { status: "ok"; items: ClientSummary[] }
  | { status: "error"; message: string; correlationId: string };

/**
 * List every client (tenant) visible to the caller. For a real
 * PLATFORM_ADMIN this is every onboarded tenant (D3: authoritative,
 * server-side, never a client guess). An empty backend list is rendered as
 * an honest empty array, never a fabricated row.
 */
export async function listClients(): Promise<ClientsResult> {
  try {
    const response = await adminApiFetch("/debug/tenants");
    const body = (await response.json()) as DebugTenantRow[];
    return { status: "ok", items: body.map(toClientSummary) };
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

export type ClientResult =
  | { status: "ok"; client: ClientSummary }
  | { status: "not_found" }
  | { status: "error"; message: string; correlationId: string };

/**
 * Resolve a single client's display name/status for the D2 context header
 * (D3: server-side, authoritative -- never derived from a link param or
 * client state). A `{tenantId}` the caller cannot see, or that doesn't
 * exist, is an honest "not_found" -- distinct from a network/backend error --
 * so the per-client layout can render a real not-found state (matching the
 * backend's own 404 on the tenant-scoped data routes, D3).
 */
export async function getClient(tenantId: string): Promise<ClientResult> {
  try {
    const response = await adminApiFetch(`/debug/tenants/${encodeURIComponent(tenantId)}`);
    // GET /debug/tenants/{id} returns `null` (200, empty body `null`) when
    // the row is missing/not visible -- TenantRepository.get returns None
    // and the route's return type is `dict[str, Any] | None`.
    const body = (await response.json()) as DebugTenantRow | null;
    if (body === null) {
      return { status: "not_found" };
    }
    return { status: "ok", client: toClientSummary(body) };
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

// ---------------------------------------------------------------------------
// Platform-level actions (D7): onboard + rotate-key. Thin wrappers only --
// the actual server actions (form parsing, Zod pre-checks, revalidation)
// live in app/(protected)/clients/actions.ts, mirroring how
// tenants/new/actions.ts calls adminApiFetch directly today. These helpers
// exist so the request bodies/response shapes are defined once, reused by
// both the clients list ("Add client") and, if wanted later, other entry
// points -- never duplicated ad hoc.
// ---------------------------------------------------------------------------

export interface OnboardClientInput {
  name: string;
  slug: string;
  adminEmail: string;
  adminName?: string;
  adminPassword?: string;
}

export interface OnboardClientResponseBody {
  tenant_id: string;
  name: string;
  slug: string;
  client_key: string;
  admin_user_id: string;
  admin_email: string;
  admin_password: string | null;
}

/**
 * Call `POST /admin/tenants` (unchanged by S12.7, PLATFORM_ADMIN-only).
 * Returns the raw response body -- the one-time `client_key`/`admin_password`
 * pass straight through to the caller and must never be logged here (secrets
 * hygiene, matching tenants/new/actions.ts). Throws `AdminApiError` on any
 * non-2xx; callers map it to field/form errors themselves (same seam as the
 * existing onboarding action).
 */
export async function onboardClient(input: OnboardClientInput): Promise<OnboardClientResponseBody> {
  const requestBody: Record<string, unknown> = {
    name: input.name,
    slug: input.slug,
    admin_email: input.adminEmail,
  };
  if (input.adminName) requestBody.admin_name = input.adminName;
  if (input.adminPassword) requestBody.admin_password = input.adminPassword;

  const response = await adminApiFetch("/admin/tenants", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(requestBody),
  });
  return (await response.json()) as OnboardClientResponseBody;
}

export interface RotateClientKeyResponseBody {
  tenant_id: string;
  client_key: string;
}

/**
 * Call `POST /admin/tenants/{tenantId}/rotate-key` (unchanged by S12.7,
 * PLATFORM_ADMIN-only). Returns the raw response body -- the fresh
 * `client_key` is one-time and must never be logged here. Throws
 * `AdminApiError` on any non-2xx (404 `TENANT_NOT_FOUND` for an unknown
 * tenant).
 */
export async function rotateClientKey(tenantId: string): Promise<RotateClientKeyResponseBody> {
  const response = await adminApiFetch(
    `/admin/tenants/${encodeURIComponent(tenantId)}/rotate-key`,
    { method: "POST" }
  );
  return (await response.json()) as RotateClientKeyResponseBody;
}

function mapErrorMessage(error: AdminApiError): string {
  if (error.status === 403 || error.errorCode === "ROLE_NOT_PERMITTED") {
    return "You do not have permission to view clients.";
  }
  if (error.status === 401) {
    return "Your session has expired. Please log in again.";
  }
  return `Something went wrong (${error.errorCode || "UNKNOWN_ERROR"}). Correlation ID: ${
    error.correlationId || "n/a"
  }.`;
}

/**
 * Server-only data layer for the Deals (opportunities) CRM page (SR-18
 * scope item 4). Clones `lib/contacts.ts`'s shape exactly: a pure,
 * unit-testable query resolver, one fetch per surface (list/detail/config),
 * a discriminated `{status:"ok"|"error"}` result, snake_case ->
 * camelCase mapping, `AdminApiError` -> an honest message + surfaced
 * correlation ID. No `tenantId` parameter anywhere (SR-17 D7) -- the
 * PLATFORM_ADMIN tenant-explicit route family
 * (`/admin/tenants/{tenantId}/opportunities`) exists on the backend but is
 * deliberately not wired by this sprint, matching every other CRM `lib/*`
 * module in this console.
 *
 * Response shapes verified live against
 * `services/api/src/api/opportunities/models.py`'s `OpportunityResponse`
 * (opportunity_id, contact_id, account_id, name, amount, currency, stage,
 * win_probability, expected_close_date, closed_at, close_reason,
 * owner_agent_id, created_at -- no `tenant_id`) and
 * `OpportunityConfigResponse` (currency, stage_probabilities).
 *
 * D6 -- MONEY HONESTY, and a real wire-format nuance worth being explicit
 * about: `OpportunityResponse.amount` is a Pydantic `Decimal | None` with no
 * custom JSON encoder configured anywhere in this codebase (verified: no
 * `model_config`/`json_encoders` override on `OpportunityResponse` or app-wide
 * in `services/api/src/api/app.py`), so FastAPI's default `jsonable_encoder`
 * serializes it as a JSON **number** on the wire, not a JSON string -- e.g.
 * `"amount": 1500.00`, not `"amount": "1500.00"`. This module's `toDeal`
 * converts that value to a `string` via `String(raw)` **immediately** at the
 * JSON -> domain-object boundary, before it is ever passed anywhere else, so
 * every layer above this file only ever sees `amount` as a `string | null`
 * and never performs (and is never tempted to perform) arithmetic on it.
 * `null` is preserved as `null` (never coerced to `"0"` or `"0.00"`) -- that
 * is the "not yet quoted" signal D6 exists to protect. This is the ONE
 * place in the frontend a JSON-numeric money value is ever touched.
 */
import "server-only";

import { adminApiFetch, AdminApiError } from "@/lib/api";

/** Fixed page size, matching the console's other CRM list pages
 * (`CONTACTS_PAGE_SIZE`, `ACCOUNTS_PAGE_SIZE`). */
export const DEALS_PAGE_SIZE = 25;

/** Mirrors `OpportunityResponse` (opportunities/models.py:63-82) exactly.
 * `amount` is kept as a `string | null` end-to-end (D6) -- `null` means
 * "not yet quoted", never "$0". `winProbability` is always a derived,
 * display-only value (SR-9.4 D2) -- there is no field anywhere in this type
 * that would let a caller send it back to the server. */
export interface Deal {
  opportunityId: string;
  contactId: string;
  accountId: string | null;
  name: string;
  amount: string | null;
  currency: string;
  stage: string;
  winProbability: number;
  expectedCloseDate: string | null;
  closedAt: string | null;
  closeReason: string | null;
  ownerAgentId: string | null;
  createdAt: string;
}

interface OpportunityResponseBody {
  opportunity_id: string;
  contact_id: string;
  account_id: string | null;
  name: string;
  amount: number | string | null;
  currency: string;
  stage: string;
  win_probability: number;
  expected_close_date: string | null;
  closed_at: string | null;
  close_reason: string | null;
  owner_agent_id: string | null;
  created_at: string;
}

interface OpportunityListResponseBody {
  items: OpportunityResponseBody[];
  total: number;
  limit: number;
  offset: number;
}

/** Mirrors `OpportunityConfigResponse` (opportunities/models.py:94-100). */
export interface DealsConfig {
  currency: string;
  stageProbabilities: Record<string, number>;
}

interface OpportunityConfigResponseBody {
  currency: string;
  stage_probabilities: Record<string, number>;
}

export type DealsResult =
  | { status: "ok"; items: Deal[]; total: number; limit: number; offset: number }
  | { status: "error"; message: string; correlationId: string };

export type DealDetailResult =
  | { status: "ok"; deal: Deal }
  | { status: "error"; message: string; correlationId: string };

export type DealsConfigResult =
  | { status: "ok"; config: DealsConfig }
  | { status: "error"; message: string; correlationId: string };

export interface DealsQueryParams {
  page: number;
  stage?: string;
  ownerAgentId?: string;
}

/**
 * Pure, unit-testable query builder (mirrors `buildContactsQuery`/
 * `buildLeadsQuery`). Clamps `page >= 1`, derives
 * `offset = (page-1) * DEALS_PAGE_SIZE`. `stage`/`ownerAgentId` are passed
 * through verbatim when present and non-blank -- the backend is the sole
 * validator (a bad `stage` value degrades to "no filter" there, not here,
 * matching `buildLeadsQuery`'s stated posture of never silently correcting a
 * value client-side into a different meaning).
 */
export function buildDealsQuery(params: DealsQueryParams): string {
  const page = Number.isFinite(params.page) && params.page >= 1 ? Math.floor(params.page) : 1;
  const offset = (page - 1) * DEALS_PAGE_SIZE;

  const query = new URLSearchParams();
  query.set("limit", String(DEALS_PAGE_SIZE));
  query.set("offset", String(offset));

  const stage = params.stage?.trim();
  if (stage) {
    query.set("stage", stage);
  }
  const ownerAgentId = params.ownerAgentId?.trim();
  if (ownerAgentId) {
    query.set("owner_agent_id", ownerAgentId);
  }

  return query.toString();
}

/** The ONE place a wire-format money value is converted -- see this file's
 * header comment. `String(raw)` is applied to a JSON number without ever
 * routing it through `Number()`/`parseFloat()`/arithmetic; `null` stays
 * `null`. */
function toAmountString(raw: number | string | null): string | null {
  if (raw === null) return null;
  return typeof raw === "string" ? raw : String(raw);
}

function toDeal(body: OpportunityResponseBody): Deal {
  return {
    opportunityId: body.opportunity_id,
    contactId: body.contact_id,
    accountId: body.account_id,
    name: body.name,
    amount: toAmountString(body.amount),
    currency: body.currency,
    stage: body.stage,
    winProbability: body.win_probability,
    expectedCloseDate: body.expected_close_date,
    closedAt: body.closed_at,
    closeReason: body.close_reason,
    ownerAgentId: body.owner_agent_id,
    createdAt: body.created_at,
  };
}

function mapErrorMessage(error: AdminApiError): string {
  if (error.status === 403 || error.errorCode === "ROLE_NOT_PERMITTED") {
    return "You do not have permission to view deals.";
  }
  if (error.status === 401) {
    return "Your session has expired. Please log in again.";
  }
  return `Something went wrong (${error.errorCode || "UNKNOWN_ERROR"}). Correlation ID: ${
    error.correlationId || "n/a"
  }.`;
}

function mapDetailErrorMessage(error: AdminApiError): string {
  if (error.status === 404 || error.errorCode === "NOT_FOUND") {
    return "This deal could not be found.";
  }
  if (error.status === 403 || error.errorCode === "ROLE_NOT_PERMITTED") {
    return "You do not have permission to view this deal.";
  }
  if (error.status === 401) {
    return "Your session has expired. Please log in again.";
  }
  return `Something went wrong (${error.errorCode || "UNKNOWN_ERROR"}). Correlation ID: ${
    error.correlationId || "n/a"
  }.`;
}

/**
 * Fetch a page of the caller's tenant deals. Never sends a `tenant_id`
 * (SR-17 D7) -- scoping is entirely the backend's repository-layer job from
 * the caller's own session cookie.
 */
export async function listDeals(params: DealsQueryParams): Promise<DealsResult> {
  const query = buildDealsQuery(params);

  try {
    const response = await adminApiFetch(`/admin/opportunities?${query}`);
    const body = (await response.json()) as OpportunityListResponseBody;
    return {
      status: "ok",
      items: body.items.map(toDeal),
      total: body.total,
      limit: body.limit,
      offset: body.offset,
    };
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

/**
 * Board-only fetch (SR-18 scope items 1/8): all of the tenant's deals
 * across every stage, in one call at the backend's max clamp (200,
 * opportunities/admin_routes.py). SR-18 D2's constrained board needs the
 * whole pipeline visible at once, not one filtered/paginated slice --
 * unlike `listDeals`, this ignores `stage`/`page` entirely. A future
 * sprint may need real pagination/virtualization once a tenant exceeds 200
 * open deals (mirrors the equivalent leads caveat in `lib/leads.ts`'s
 * board fetch -- deliberately not pre-built, "measure first").
 */
export async function listAllDealsForBoard(): Promise<DealsResult> {
  try {
    const response = await adminApiFetch(`/admin/opportunities?limit=200&offset=0`);
    const body = (await response.json()) as OpportunityListResponseBody;
    return {
      status: "ok",
      items: body.items.map(toDeal),
      total: body.total,
      limit: body.limit,
      offset: body.offset,
    };
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

/** Fetch a single deal's detail for the record drawer. */
export async function getDealDetail(opportunityId: string): Promise<DealDetailResult> {
  try {
    const response = await adminApiFetch(`/admin/opportunities/${encodeURIComponent(opportunityId)}`);
    const body = (await response.json()) as OpportunityResponseBody;
    return { status: "ok", deal: toDeal(body) };
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

/**
 * Fetch the tenant's opportunity config (currency + per-stage
 * win-probabilities). `GET /admin/opportunities/config` is readable by both
 * `CLIENT_ADMIN` and `CLIENT_AGENT` (SR-9.4 D8) -- this is the one
 * sanctioned cross-module read this page needs (a config resolver, not a
 * second data source), used only to render the tenant's currency code
 * consistently even for a deal whose own `currency` field might be absent
 * (it never is, but this is the fallback source of truth for page-level
 * copy like the "Add deal" form's currency label). `PUT /config` is never
 * called by this module (CLIENT_ADMIN-only, out of scope for this sprint).
 */
export async function getDealsConfig(): Promise<DealsConfigResult> {
  try {
    const response = await adminApiFetch("/admin/opportunities/config");
    const body = (await response.json()) as OpportunityConfigResponseBody;
    return {
      status: "ok",
      config: { currency: body.currency, stageProbabilities: body.stage_probabilities },
    };
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

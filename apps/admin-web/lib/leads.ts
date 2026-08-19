/**
 * Server-only data layer for the lead review console (S13.4). Builds the
 * `GET /admin/leads` query string, calls `adminApiFetch`, and maps the
 * response (or any error) into a discriminated `LeadsResult` the page
 * renders directly -- no silent fallbacks (CLAUDE.md §3): a backend error
 * always becomes a visible, honest state, never a blank/faked table.
 *
 * Constants below are sourced verbatim from the real backend, not invented:
 *  - `LEAD_STAGES` mirrors `STAGE_ORDER ∪ TERMINAL_STAGES`
 *    (services/api/src/api/leads/pipeline.py:38-41).
 *  - `LEAD_STATUSES` mirrors `_STATUS_BY_STAGE.values()` (pipeline.py:44-50).
 *  - `LEADS_PAGE_SIZE` is a UI choice (S13.4 decision 3, Q1 default 25),
 *    well within the backend's `[1,200]` clamp (admin_routes.py:236).
 */
import "server-only";

import { adminApiFetch, AdminApiError } from "@/lib/api";
import type {
  LeadListItem,
  LeadDetail,
  LeadDetailResult,
  LeadActivityItem,
  LeadActivitiesResult,
  BadgeStyle,
} from "@/lib/leads-presentation";
import {
  stageBadgeStyle,
  scoreChipStyle,
  initialsFromName,
} from "@/lib/leads-presentation";

// Re-exported so existing server-side consumers (`leads-table.tsx`,
// `lib/dashboard.ts`, and this module's own test suite) can keep importing
// these pure, client-safe types/functions from `@/lib/leads` unchanged.
// `lead-drawer.tsx` (a Client Component) must import them from
// `@/lib/leads-presentation` directly instead -- see that file's header for
// why (this module is `server-only` and can't be reached from client code,
// even transitively for a type-only import, without triggering Next's
// "'server-only' cannot be imported from a Client Component" build error).
export type {
  LeadListItem,
  LeadDetail,
  LeadDetailResult,
  LeadActivityItem,
  LeadActivitiesResult,
  BadgeStyle,
};
export { stageBadgeStyle, scoreChipStyle, initialsFromName };

/** The five canonical lead stages (pipeline.py STAGE_ORDER + TERMINAL_STAGES). */
export const LEAD_STAGES = [
  "captured",
  "qualified",
  "contacted",
  "converted",
  "disqualified",
] as const;

export type LeadStage = (typeof LEAD_STAGES)[number];

/** The four canonical lead statuses (pipeline.py _STATUS_BY_STAGE values). */
export const LEAD_STATUSES = ["new", "open", "won", "lost"] as const;

export type LeadStatus = (typeof LEAD_STATUSES)[number];

/** Closed mirror of api.leads.repository's SR-25 ORDER BY allowlist. */
export const LEAD_SORT_KEYS = [
  "name",
  "email",
  "stage",
  "status",
  "score",
  "assigned",
  "created",
] as const;

export type LeadSortKey = (typeof LEAD_SORT_KEYS)[number];
export type LeadSortDirection = "asc" | "desc";

const LEAD_SORT_DEFAULT_DIRECTIONS: Record<LeadSortKey, LeadSortDirection> = {
  name: "asc",
  email: "asc",
  stage: "asc",
  status: "asc",
  score: "desc",
  assigned: "asc",
  created: "desc",
};

export function defaultLeadSortDirection(sort: LeadSortKey): LeadSortDirection {
  return LEAD_SORT_DEFAULT_DIRECTIONS[sort];
}

/** Fixed page size for the console's Prev/Next pagination (decision 3, Q1). */
export const LEADS_PAGE_SIZE = 25;

// `LeadListItem` now lives in `@/lib/leads-presentation` (client-safe) and is
// re-exported at the top of this file -- see that declaration's comment for
// why. Server-side callers importing it from `@/lib/leads` are unaffected.

interface LeadListItemResponseBody {
  lead_id: string;
  name: string | null;
  email: string | null;
  phone: string | null;
  status: string;
  stage: string;
  qualification_score: number | null;
  assigned_agent_id: string | null;
  source: string;
  created_at: string;
}

interface LeadListResponseBody {
  items: LeadListItemResponseBody[];
  total: number;
  limit: number;
  offset: number;
}

export type LeadsResult =
  | { status: "ok"; items: LeadListItem[]; total: number; limit: number; offset: number }
  | { status: "error"; message: string; correlationId: string };

export interface LeadsQueryParams {
  page: number;
  stage?: string;
  status?: string;
  assignedAgentId?: string;
  createdFrom?: string;
  createdTo?: string;
  q?: string;
  sort?: string;
  direction?: string;
}

/**
 * Pure, unit-testable query builder (decision 3/5). Clamps `page >= 1`,
 * derives `offset = (page-1) * LEADS_PAGE_SIZE`, and drops any `stage` value
 * not in `LEAD_STAGES` (unknown/blank -> no stage filter at all, so a
 * stray/hand-edited `?stage=bogus` degrades to "no filter" rather than a
 * hard 422 on first paint -- Decision 5's belt-and-suspenders is the 422
 * mapping in `listLeads` below, for the rare case a bad value still reaches
 * the backend).
 */
export function buildLeadsQuery(params: LeadsQueryParams): string {
  const page = Number.isFinite(params.page) && params.page >= 1 ? Math.floor(params.page) : 1;
  const offset = (page - 1) * LEADS_PAGE_SIZE;

  const query = new URLSearchParams();
  query.set("limit", String(LEADS_PAGE_SIZE));
  query.set("offset", String(offset));

  const stage = params.stage?.trim();
  if (stage && (LEAD_STAGES as readonly string[]).includes(stage)) {
    query.set("stage", stage);
  }

  const status = params.status?.trim();
  if (status && (LEAD_STATUSES as readonly string[]).includes(status)) {
    query.set("status", status);
  }

  const assignedAgentId = params.assignedAgentId?.trim();
  if (assignedAgentId && assignedAgentId.length <= 200) {
    query.set("assigned_agent_id", assignedAgentId);
  }

  for (const [key, value] of [
    ["from", params.createdFrom],
    ["to", params.createdTo],
  ] as const) {
    const normalized = value?.trim();
    if (normalized && !Number.isNaN(Date.parse(normalized))) {
      query.set(key, normalized);
    }
  }

  const q = params.q?.trim();
  if (q && q.length >= 2 && q.length <= 200) {
    query.set("q", q);
  }

  const sort = params.sort?.trim();
  if (sort && (LEAD_SORT_KEYS as readonly string[]).includes(sort)) {
    const sortKey = sort as LeadSortKey;
    query.set("sort", sortKey);
    const direction = params.direction?.trim();
    query.set(
      "dir",
      direction === "asc" || direction === "desc"
        ? direction
        : LEAD_SORT_DEFAULT_DIRECTIONS[sortKey]
    );
  }

  return query.toString();
}

function toLeadListItem(body: LeadListItemResponseBody): LeadListItem {
  return {
    leadId: body.lead_id,
    name: body.name,
    email: body.email,
    phone: body.phone,
    status: body.status,
    stage: body.stage,
    qualificationScore: body.qualification_score,
    assignedAgentId: body.assigned_agent_id,
    source: body.source,
    createdAt: body.created_at,
  };
}

/**
 * Fetch a page of the caller's tenant leads. Never sends a `tenant_id` --
 * scoping is entirely the backend's repository-layer job from the caller's
 * own claims (CLAUDE.md §3). Never logs the response body (PII-minimal).
 *
 * `tenantId` (S13.7): when provided, targets the S12.7 PLATFORM_ADMIN
 * super-user surface `GET /admin/tenants/{tenantId}/leads` instead of the
 * implicit `GET /admin/leads` -- same response shape
 * (admin_routes.py `_list_leads`), only the URL prefix differs. Always the
 * route segment's `{tenantId}`, never client state (D1).
 */
export async function listLeads(
  params: LeadsQueryParams,
  tenantId?: string
): Promise<LeadsResult> {
  const query = buildLeadsQuery(params);
  const basePath = tenantId
    ? `/admin/tenants/${encodeURIComponent(tenantId)}/leads`
    : "/admin/leads";

  try {
    const response = await adminApiFetch(`${basePath}?${query}`);
    const body = (await response.json()) as LeadListResponseBody;
    return {
      status: "ok",
      items: body.items.map(toLeadListItem),
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
    return "You do not have permission to review leads.";
  }
  if (error.status === 401) {
    return "Your session has expired. Please log in again.";
  }
  if (
    error.errorCode === "INVALID_LEAD_FILTER" ||
    error.errorCode === "INVALID_LIST_WINDOW" ||
    error.errorCode === "INVALID_LEAD_SORT" ||
    error.errorCode === "INVALID_LEAD_SORT_DIRECTION" ||
    error.errorCode === "INVALID_LEAD_SEARCH"
  ) {
    return "That filter isn't valid -- showing all leads.";
  }
  return `Something went wrong (${error.errorCode || "UNKNOWN_ERROR"}). Correlation ID: ${
    error.correlationId || "n/a"
  }.`;
}

/**
 * Board-only fetch (SR-18 scope items 2/8): all of the tenant's leads
 * across every stage, in one call at the backend's max clamp (200,
 * leads/admin_routes.py). SR-18 D2's constrained board needs to see every
 * stage at once (a card's legal targets are other columns on the SAME
 * board) -- unlike `listLeads`, this ignores `stage`/`page` entirely and is
 * not paginated. A future sprint may need real pagination/virtualization
 * once tenants exceed 200 open leads (SR-18 spec's open question "does the
 * board scale past a few hundred cards per column?" -- deliberately not
 * pre-built here, per that question's own "measure first" guidance).
 */
export async function listAllLeadsForBoard(): Promise<LeadsResult> {
  try {
    // include_converted=true (bug fix): the backend excludes converted
    // leads by default (SR-9.2 D1 -- a sensible default for the Table
    // view's working list), but the Board's Converted column IS the UI
    // for viewing conversion history -- without this, that column would
    // structurally always show 0/empty no matter how many leads get
    // converted, even though the conversion itself (and the resulting
    // Contact) succeeded.
    const response = await adminApiFetch(`/admin/leads?limit=200&offset=0&include_converted=true`);
    const body = (await response.json()) as LeadListResponseBody;
    return {
      status: "ok",
      items: body.items.map(toLeadListItem),
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

function mapDetailErrorMessage(error: AdminApiError): string {
  if (error.status === 404 || error.errorCode === "NOT_FOUND") {
    return "This lead could not be found.";
  }
  if (error.status === 403 || error.errorCode === "ROLE_NOT_PERMITTED") {
    return "You do not have permission to view this lead.";
  }
  if (error.status === 401) {
    return "Your session has expired. Please log in again.";
  }
  return `Something went wrong (${error.errorCode || "UNKNOWN_ERROR"}). Correlation ID: ${
    error.correlationId || "n/a"
  }.`;
}

// ---------------------------------------------------------------------------
// Lead detail drawer (4b) -- GET /admin/leads/{lead_id} and
// GET /admin/leads/{lead_id}/activities. Both mirror `admin_routes.py`'s
// leak-free response shapes exactly (no `tenant_id`). There is intentionally
// no transcript endpoint in this router (leads are not linked to a
// conversation_id anywhere server-side yet) -- the drawer's Transcript tab
// therefore has no real data source and must show an honest "not available"
// state rather than fabricate one (CLAUDE.md §3, no silent fallbacks).
// ---------------------------------------------------------------------------

interface LeadDetailResponseBody {
  lead_id: string;
  name: string;
  email: string;
  phone: string | null;
  status: string;
  stage: string;
  qualification_score: number | null;
  assigned_agent_id: string | null;
  source: string;
}

function toLeadDetail(body: LeadDetailResponseBody): LeadDetail {
  return {
    leadId: body.lead_id,
    name: body.name,
    email: body.email,
    phone: body.phone,
    status: body.status,
    stage: body.stage,
    qualificationScore: body.qualification_score,
    assignedAgentId: body.assigned_agent_id,
    source: body.source,
  };
}

/** Fetch a single lead's detail for the drawer's Details tab. Mirrors
 * `listLeads`'s tenant-scoped-path convention exactly. */
export async function getLeadDetail(
  leadId: string,
  tenantId?: string
): Promise<LeadDetailResult> {
  const basePath = tenantId
    ? `/admin/tenants/${encodeURIComponent(tenantId)}/leads`
    : "/admin/leads";

  try {
    const response = await adminApiFetch(`${basePath}/${encodeURIComponent(leadId)}`);
    const body = (await response.json()) as LeadDetailResponseBody;
    return { status: "ok", lead: toLeadDetail(body) };
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

interface LeadActivityResponseBody {
  activity_id: string;
  lead_id: string;
  type: string;
  payload: Record<string, unknown> | null;
  actor: string | null;
  created_at: string;
}

function toLeadActivityItem(body: LeadActivityResponseBody): LeadActivityItem {
  return {
    activityId: body.activity_id,
    leadId: body.lead_id,
    type: body.type,
    payload: body.payload,
    actor: body.actor,
    createdAt: body.created_at,
  };
}

/** Fetch a lead's full timeline for the drawer's Activity tab. The Notes tab
 * reuses this same call, filtered client-side to `type === "note"` -- the
 * backend has no separate notes-only GET route. */
export async function getLeadActivities(
  leadId: string,
  tenantId?: string
): Promise<LeadActivitiesResult> {
  const basePath = tenantId
    ? `/admin/tenants/${encodeURIComponent(tenantId)}/leads`
    : "/admin/leads";

  try {
    const response = await adminApiFetch(
      `${basePath}/${encodeURIComponent(leadId)}/activities`
    );
    const body = (await response.json()) as LeadActivityResponseBody[];
    return { status: "ok", items: body.map(toLeadActivityItem) };
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

// 4b design tokens (badge/score/initials helpers) now live in
// `@/lib/leads-presentation` (client-safe) and are re-exported at the top of
// this file for backwards compatibility with existing server-side imports.

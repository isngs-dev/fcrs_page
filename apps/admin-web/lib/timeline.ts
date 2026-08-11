/**
 * Server-only data layer serving BOTH timeline endpoints (D3):
 * `GET /admin/contacts/{contact_id}/timeline` and
 * `GET /admin/leads/{lead_id}/timeline`. One module, one mapping function,
 * because the backend itself delegates both routes to a single `_impl`
 * (`timeline/admin_routes.py:4-10`) and differs only in which identity
 * resolver ran -- the wire shape (`TimelineResponse`) is identical.
 *
 * D4 (LOCKED, the highest-stakes rule in this sprint): the `degraded` flag
 * and each source's per-source `state` MUST survive this mapping layer
 * verbatim. `toTimelineResult` below performs a structural, field-by-field
 * copy with no branch that could coerce `degraded: true` into `false` or
 * drop a failed source from `sources` -- see the dedicated test in
 * `lib/__tests__/timeline.test.ts` asserting this explicitly on a
 * fixture with one source failed.
 *
 * Response shape verified live against
 * `services/api/src/api/timeline/models.py`'s `TimelineResponse`/
 * `TimelineSourceState`/`TimelineItemResponse` (via `admin_routes.py:60-78`
 * `_to_response`): `subject{kind,id,converted_to_contact_id}`,
 * `degraded: bool`, `sources: {name: {state,count,truncated}}`,
 * `items: [{kind,occurred_at,id,data}]`, `next_before`.
 */
import "server-only";

import { adminApiFetch, AdminApiError } from "@/lib/api";

export type TimelineSubjectKind = "contact" | "lead";

export interface TimelineSubject {
  kind: TimelineSubjectKind;
  id: string;
  convertedToContactId: string | null;
}

/** Per-source fan-out state -- mirrors `TimelineSourceState` exactly.
 * `state` is a free-form string from the backend (e.g. "ok" | "failed" |
 * "empty") and is NOT narrowed/validated here -- narrowing it would risk
 * silently swallowing a future state value the UI doesn't yet know about
 * (no-silent-fallback, D4). */
export interface TimelineSourceState {
  state: string;
  count: number;
  truncated: boolean;
}

export interface TimelineItem {
  kind: string;
  occurredAt: string;
  id: string;
  data: Record<string, unknown>;
}

/** Mirrors `TimelineResponse` verbatim, field-by-field -- `degraded` and
 * `sources` are never flattened, dropped, or re-derived (D4). */
export interface TimelineResult {
  subject: TimelineSubject;
  degraded: boolean;
  sources: Record<string, TimelineSourceState>;
  items: TimelineItem[];
  nextBefore: string | null;
}

interface TimelineSubjectBody {
  kind: string;
  id: string;
  converted_to_contact_id: string | null;
}

interface TimelineSourceStateBody {
  state: string;
  count: number;
  truncated: boolean;
}

interface TimelineItemBody {
  kind: string;
  occurred_at: string;
  id: string;
  data: Record<string, unknown>;
}

interface TimelineResponseBody {
  subject: TimelineSubjectBody;
  degraded: boolean;
  sources: Record<string, TimelineSourceStateBody>;
  items: TimelineItemBody[];
  next_before: string | null;
}

export type TimelineFetchResult =
  | { status: "ok"; data: TimelineResult }
  | { status: "error"; message: string; correlationId: string };

export interface TimelineQueryParams {
  before?: string;
  limit?: number;
}

/**
 * Pure, unit-testable query builder. `before` is passed through verbatim
 * when present (an ISO datetime string, per the endpoint's cursor
 * pagination contract) and omitted when absent -- no client-side default
 * cursor is invented. `limit` is optional; when omitted the backend's own
 * default (50) and `[1,200]` clamp apply (mirrors D6's stance on accounts'
 * clamp: the client does not re-implement or second-guess it).
 */
export function buildTimelineQuery(params: TimelineQueryParams): string {
  const query = new URLSearchParams();
  if (params.before) {
    query.set("before", params.before);
  }
  if (params.limit !== undefined && Number.isFinite(params.limit)) {
    query.set("limit", String(Math.floor(params.limit)));
  }
  return query.toString();
}

/**
 * Structural mapping, snake_case -> camelCase, with NO branch that can
 * change the truth value of `degraded` or omit a `sources` entry (D4). A
 * `degraded: true` in, is a `degraded: true` out, always.
 */
export function toTimelineResult(body: TimelineResponseBody): TimelineResult {
  const sources: Record<string, TimelineSourceState> = {};
  for (const [name, outcome] of Object.entries(body.sources)) {
    sources[name] = {
      state: outcome.state,
      count: outcome.count,
      truncated: outcome.truncated,
    };
  }

  return {
    subject: {
      kind: body.subject.kind as TimelineSubjectKind,
      id: body.subject.id,
      convertedToContactId: body.subject.converted_to_contact_id,
    },
    // Verbatim, no coercion -- the load-bearing line of this module (D4).
    degraded: body.degraded,
    sources,
    items: body.items.map((item) => ({
      kind: item.kind,
      occurredAt: item.occurred_at,
      id: item.id,
      data: item.data,
    })),
    nextBefore: body.next_before,
  };
}

function mapErrorMessage(error: AdminApiError): string {
  if (error.status === 404 || error.errorCode === "NOT_FOUND") {
    return "This record could not be found.";
  }
  if (error.status === 403 || error.errorCode === "ROLE_NOT_PERMITTED") {
    return "You do not have permission to view this timeline.";
  }
  if (error.status === 401) {
    return "Your session has expired. Please log in again.";
  }
  return `Something went wrong (${error.errorCode || "UNKNOWN_ERROR"}). Correlation ID: ${
    error.correlationId || "n/a"
  }.`;
}

async function fetchTimeline(path: string, params: TimelineQueryParams): Promise<TimelineFetchResult> {
  const query = buildTimelineQuery(params);
  const url = query ? `${path}?${query}` : path;

  try {
    const response = await adminApiFetch(url);
    const body = (await response.json()) as TimelineResponseBody;
    return { status: "ok", data: toTimelineResult(body) };
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

/** `GET /admin/contacts/{contact_id}/timeline`. Never sends `tenant_id`
 * (D7). Never logs the response body (PII-minimal; timeline items may
 * carry conversation/lead content). */
export async function getContactTimeline(
  contactId: string,
  params: TimelineQueryParams = {}
): Promise<TimelineFetchResult> {
  return fetchTimeline(`/admin/contacts/${encodeURIComponent(contactId)}/timeline`, params);
}

/** `GET /admin/leads/{lead_id}/timeline`. Never sends `tenant_id` (D7). */
export async function getLeadTimeline(
  leadId: string,
  params: TimelineQueryParams = {}
): Promise<TimelineFetchResult> {
  return fetchTimeline(`/admin/leads/${encodeURIComponent(leadId)}/timeline`, params);
}

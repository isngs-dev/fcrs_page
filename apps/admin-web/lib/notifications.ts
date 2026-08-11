/**
 * Server-only data layer for the notifications feed page (SR-21). Mirrors
 * `lib/contacts.ts`/`lib/leads.ts`'s established shape (SR-17 M9): a pure,
 * unit-testable query resolver, one fetch per surface (list), a
 * discriminated `{status:"ok"|"error"}` result, snake_case -> camelCase
 * mapping, and `AdminApiError` -> an honest message + surfaced correlation
 * ID. No `tenantId` parameter anywhere (SR-17 D7) -- the PLATFORM_ADMIN
 * tenant-explicit route family exists on the backend
 * (`/admin/tenants/{tenant_id}/notifications`) but is deliberately not
 * wired by this console surface, matching every other SR-17+ `lib/*.ts`.
 *
 * Response shape verified live against
 * `services/api/src/api/notifications/admin_routes.py`'s
 * `NotificationEventListResponse`/`NotificationEventResponse` (SR-21):
 * items[] carry event_id/kind/category/target_type/target_id/payload/
 * actor_id/created_at/read, plus total/unread_count/limit/offset -- no
 * `tenant_id` ever (D6's per-caller `read` flag is resolved server-side).
 *
 * D4: exactly two live categories -- "leads" and "system". There is no
 * "mentions" category anywhere in this file, the API, or the UI it backs --
 * a deliberate, documented deviation from the four-pill mockup (see
 * `app/(protected)/notifications/page.tsx`'s docstring).
 */
import "server-only";

import { adminApiFetch, AdminApiError } from "@/lib/api";

/** D4: the feed's closed category vocabulary. `undefined` means "All". */
export type NotificationCategory = "leads" | "system";

/** Fixed page size for the feed, matching the backend's list default. */
export const NOTIFICATIONS_PAGE_SIZE = 50;

/** Mirrors `NotificationEventResponse` (notifications/admin_routes.py) exactly. */
export interface NotificationEvent {
  eventId: string;
  kind: string;
  category: NotificationCategory | string;
  targetType: string | null;
  targetId: string | null;
  payload: Record<string, unknown> | null;
  actorId: string | null;
  createdAt: string;
  read: boolean;
}

interface NotificationEventResponseBody {
  event_id: string;
  kind: string;
  category: string;
  target_type: string | null;
  target_id: string | null;
  payload: Record<string, unknown> | null;
  actor_id: string | null;
  created_at: string;
  read: boolean;
}

interface NotificationEventListResponseBody {
  items: NotificationEventResponseBody[];
  total: number;
  unread_count: number;
  limit: number;
  offset: number;
}

export type NotificationsResult =
  | {
      status: "ok";
      items: NotificationEvent[];
      total: number;
      unreadCount: number;
      limit: number;
      offset: number;
    }
  | { status: "error"; message: string; correlationId: string };

export interface NotificationsQueryParams {
  category?: NotificationCategory;
  unreadOnly?: boolean;
  page: number;
}

/**
 * Pure, unit-testable query builder (mirrors `buildContactsQuery`). Clamps
 * `page >= 1`, derives `offset = (page-1) * NOTIFICATIONS_PAGE_SIZE`.
 * `limit` is always sent as `NOTIFICATIONS_PAGE_SIZE` -- the backend
 * additionally clamps `[1,200]` server-side (D7), which this function does
 * not re-implement or second-guess.
 */
export function buildNotificationsQuery(params: NotificationsQueryParams): string {
  const page = Number.isFinite(params.page) && params.page >= 1 ? Math.floor(params.page) : 1;
  const offset = (page - 1) * NOTIFICATIONS_PAGE_SIZE;

  const query = new URLSearchParams();
  query.set("limit", String(NOTIFICATIONS_PAGE_SIZE));
  query.set("offset", String(offset));
  if (params.category) {
    query.set("category", params.category);
  }
  if (params.unreadOnly) {
    query.set("unread_only", "true");
  }

  return query.toString();
}

function toNotificationEvent(body: NotificationEventResponseBody): NotificationEvent {
  return {
    eventId: body.event_id,
    kind: body.kind,
    category: body.category,
    targetType: body.target_type,
    targetId: body.target_id,
    payload: body.payload,
    actorId: body.actor_id,
    createdAt: body.created_at,
    read: body.read,
  };
}

function mapErrorMessage(error: AdminApiError): string {
  if (error.status === 403 || error.errorCode === "ROLE_NOT_PERMITTED") {
    return "You do not have permission to view notifications.";
  }
  if (error.status === 401) {
    return "Your session has expired. Please log in again.";
  }
  return `Something went wrong (${error.errorCode || "UNKNOWN_ERROR"}). Correlation ID: ${
    error.correlationId || "n/a"
  }.`;
}

/**
 * Fetch a page of the caller's tenant notification feed. Never sends a
 * `tenant_id` -- scoping and the per-caller `read` flag are entirely the
 * backend's job from the caller's own session cookie (D6).
 */
export async function listNotifications(
  params: NotificationsQueryParams
): Promise<NotificationsResult> {
  const query = buildNotificationsQuery(params);

  try {
    const response = await adminApiFetch(`/admin/notifications?${query}`);
    const body = (await response.json()) as NotificationEventListResponseBody;
    return {
      status: "ok",
      items: body.items.map(toNotificationEvent),
      total: body.total,
      unreadCount: body.unread_count,
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
 * Resolve the console deep-link href for a feed item's target entity. A
 * `lead`/`contact` target opens the existing leads/contacts drawer via its
 * established `?<entity>=<id>` URL-state convention (mirrors
 * `app/(protected)/leads/page.tsx`'s `?lead=<id>`); a `conversation` target
 * opens the conversations page; an `ingestion_run` target (no dedicated
 * detail view yet) falls back to the Knowledge page. Returns `null` when
 * there is nothing sensible to link to (e.g. a missing target_id) -- the UI
 * renders the item as plain text rather than a broken link.
 */
export function notificationTargetHref(event: NotificationEvent): string | null {
  if (!event.targetId) return null;
  switch (event.targetType) {
    case "lead":
      return `/leads?lead=${encodeURIComponent(event.targetId)}`;
    case "contact":
      return `/contacts?contact=${encodeURIComponent(event.targetId)}`;
    case "conversation":
      return `/conversations?conversation=${encodeURIComponent(event.targetId)}`;
    case "ingestion_run":
      return "/knowledge";
    default:
      return null;
  }
}

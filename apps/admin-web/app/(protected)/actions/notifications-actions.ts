"use server";

/**
 * SR-21 scope item 10: server actions for the notifications feed's mutation
 * surface -- mark one item read/unread, and "mark all read" (optionally
 * scoped to a category). Mirrors `pipeline-actions.ts`'s posture (thin,
 * POST/PATCH straight through, revalidate the page path on success) and
 * `lib/notifications.ts`'s discriminated result shape.
 *
 * Both actions target the per-CALLER read state only (D6) -- there is no
 * `userId` parameter anywhere in this file; the backend resolves the acting
 * user from the session cookie, exactly like every other admin mutation in
 * this console.
 */
import { revalidatePath } from "next/cache";
import { AdminApiError, adminApiFetch } from "@/lib/api";

export interface NotificationsActionResult {
  status: "ok" | "error";
  message?: string;
  correlationId?: string;
}

function mapError(error: AdminApiError): string {
  if (error.status === 404 || error.errorCode === "NOT_FOUND") {
    return "This notification could not be found.";
  }
  if (error.status === 403 || error.errorCode === "ROLE_NOT_PERMITTED") {
    return "You do not have permission to update notifications.";
  }
  if (error.status === 401) {
    return "Your session has expired. Please log in again.";
  }
  return `${error.message} (correlation ID: ${error.correlationId || "unknown"}).`;
}

const GENERIC_NETWORK_ERROR = "Unable to reach the server. Please try again.";

/** PATCH /admin/notifications/{event_id}/read {read} */
export async function markNotificationRead(
  eventId: string,
  read: boolean,
  revalidatePathTarget: string
): Promise<NotificationsActionResult> {
  try {
    await adminApiFetch(`/admin/notifications/${encodeURIComponent(eventId)}/read`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ read }),
    });
    revalidatePath(revalidatePathTarget);
    return { status: "ok" };
  } catch (error) {
    if (error instanceof AdminApiError) {
      return { status: "error", message: mapError(error), correlationId: error.correlationId };
    }
    return { status: "error", message: GENERIC_NETWORK_ERROR, correlationId: "" };
  }
}

/** PATCH /admin/notifications/read-all {category?} -- affects only the
 * caller's own read rows (D6); `category` omitted means every category. */
export async function markAllNotificationsRead(
  category: "leads" | "system" | undefined,
  revalidatePathTarget: string
): Promise<NotificationsActionResult> {
  try {
    await adminApiFetch("/admin/notifications/read-all", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ category: category ?? null }),
    });
    revalidatePath(revalidatePathTarget);
    return { status: "ok" };
  } catch (error) {
    if (error instanceof AdminApiError) {
      return { status: "error", message: mapError(error), correlationId: error.correlationId };
    }
    return { status: "error", message: GENERIC_NETWORK_ERROR, correlationId: "" };
  }
}

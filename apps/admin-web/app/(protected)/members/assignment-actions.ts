"use server";

/**
 * Round-robin toggle server action (SR-20 D1/D2). Calls
 * `PUT /admin/assignment-config`. Confirmed, not optimistic: the returned
 * state reflects the backend's response.
 */
import { revalidatePath } from "next/cache";
import { AdminApiError, adminApiFetch } from "@/lib/api";

export interface SetAssignmentConfigResult {
  status: "ok" | "error";
  roundRobinEnabled?: boolean;
  message?: string;
  correlationId?: string;
}

interface AssignmentConfigResponseBody {
  round_robin_enabled: boolean;
}

export async function setAssignmentConfigAction(
  roundRobinEnabled: boolean
): Promise<SetAssignmentConfigResult> {
  try {
    const response = await adminApiFetch("/admin/assignment-config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ round_robin_enabled: roundRobinEnabled }),
    });
    const body = (await response.json()) as AssignmentConfigResponseBody;
    revalidatePath("/members");
    return { status: "ok", roundRobinEnabled: body.round_robin_enabled };
  } catch (err) {
    if (err instanceof AdminApiError) {
      if (err.status === 403 || err.errorCode === "ROLE_NOT_PERMITTED") {
        return {
          status: "error",
          message: "You do not have permission to change auto-assignment.",
          correlationId: err.correlationId,
        };
      }
      if (err.status === 401) {
        return {
          status: "error",
          message: "Your session has expired. Please sign in again.",
          correlationId: err.correlationId,
        };
      }
      return { status: "error", message: err.message, correlationId: err.correlationId };
    }
    return {
      status: "error",
      message: "Unable to reach the server. Please try again.",
      correlationId: "",
    };
  }
}

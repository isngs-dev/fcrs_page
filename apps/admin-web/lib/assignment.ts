/**
 * Server-only data layer for round-robin lead assignment (SR-20 D1/D2).
 * `getAssignmentConfig()` calls `GET /admin/assignment-config`; the
 * corresponding PUT is wired via `members/assignment-actions.ts`'s
 * `setAssignmentConfig` server action (mutations live in `actions.ts` files
 * per the codebase's convention, reads live here per `lib/settings.ts`'s
 * precedent).
 */
import "server-only";

import { adminApiFetch, AdminApiError } from "@/lib/api";

export interface AssignmentConfig {
  roundRobinEnabled: boolean;
}

interface AssignmentConfigResponseBody {
  round_robin_enabled: boolean;
}

export type AssignmentConfigResult =
  | { status: "ok"; config: AssignmentConfig }
  | { status: "error"; message: string; correlationId: string };

export async function getAssignmentConfig(): Promise<AssignmentConfigResult> {
  try {
    const response = await adminApiFetch("/admin/assignment-config");
    const body = (await response.json()) as AssignmentConfigResponseBody;
    return { status: "ok", config: { roundRobinEnabled: body.round_robin_enabled } };
  } catch (error) {
    if (error instanceof AdminApiError) {
      return {
        status: "error",
        message: mapErrorMessage(error),
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

function mapErrorMessage(error: AdminApiError): string {
  if (error.status === 403 || error.errorCode === "ROLE_NOT_PERMITTED") {
    return "You do not have permission to view auto-assignment settings.";
  }
  if (error.status === 401) {
    return "Your session has expired. Please log in again.";
  }
  return `Something went wrong (${error.errorCode || "UNKNOWN_ERROR"}). Correlation ID: ${
    error.correlationId || "n/a"
  }.`;
}

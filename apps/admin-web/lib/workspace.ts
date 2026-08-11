/**
 * Server-only data layer for the Workspace settings screen (SR-20 D5).
 * Mirrors `lib/settings.ts`'s shape exactly: a typed `getWorkspace()`
 * calling `GET /admin/workspace` via `adminApiFetch`, mapping the response
 * (or any error) into a discriminated `WorkspaceResult` -- no silent
 * fallbacks (CLAUDE.md §3): a backend error always becomes a visible,
 * honest state, never a blank/faked form.
 *
 * D5 amended: no `language` field anywhere here -- Language renders
 * disabled/coming-soon in the Settings UI (like Billing/Delete-workspace),
 * not a live stored field. See `services/api/src/api/admin/
 * workspace_repository.py`'s module docstring for the full rationale.
 */
import "server-only";

import { adminApiFetch, AdminApiError } from "@/lib/api";

export interface Workspace {
  name: string;
  slug: string;
  timezone: string;
}

interface AdminWorkspaceResponseBody {
  name: string;
  slug: string;
  timezone: string;
}

export type WorkspaceResult =
  | { status: "ok"; workspace: Workspace }
  | { status: "error"; message: string; correlationId: string };

export async function getWorkspace(): Promise<WorkspaceResult> {
  try {
    const response = await adminApiFetch("/admin/workspace");
    const body = (await response.json()) as AdminWorkspaceResponseBody;
    return { status: "ok", workspace: body };
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
    return "You do not have permission to view workspace settings.";
  }
  if (error.status === 401) {
    return "Your session has expired. Please log in again.";
  }
  return `Something went wrong (${error.errorCode || "UNKNOWN_ERROR"}). Correlation ID: ${
    error.correlationId || "n/a"
  }.`;
}

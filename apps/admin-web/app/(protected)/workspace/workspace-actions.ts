"use server";

/**
 * Workspace settings save action (SR-20 D5). Validates with a light Zod
 * pre-check (name/slug shape; timezone/language are validated authoritatively
 * by the backend, IANA-checked in Python before its own DB write), then
 * calls `PUT /admin/workspace` via `adminApiFetch`.
 *
 * Confirmed, not optimistic (mirrors `settings/actions.ts`'s pattern): on a
 * 200, the returned state carries the PUT response body's fresh values.
 * `revalidatePath("/workspace")` re-syncs the RSC (moved from `/settings`
 * when the workspace-settings screen split into its own route).
 */
import { revalidatePath } from "next/cache";
import { z } from "zod";
import { AdminApiError, adminApiFetch } from "@/lib/api";
import type { Workspace } from "@/lib/workspace";

const workspaceFormSchema = z.object({
  name: z.string().trim().min(1, "Workspace name is required.").max(200),
  slug: z
    .string()
    .trim()
    .min(1, "Workspace URL is required.")
    .max(63, "Workspace URL must be 63 characters or fewer."),
  timezone: z.string().trim().optional(),
});

export interface SaveWorkspaceFieldErrors {
  name?: string;
  slug?: string;
  timezone?: string;
}

export interface SaveWorkspaceIdleState {
  status: "idle";
}

export interface SaveWorkspaceErrorState {
  status: "error";
  fieldErrors: SaveWorkspaceFieldErrors;
  formError: string | null;
  correlationId: string | null;
}

export interface SaveWorkspaceSuccessState {
  status: "saved";
  workspace: Workspace;
}

export type SaveWorkspaceState =
  | SaveWorkspaceIdleState
  | SaveWorkspaceErrorState
  | SaveWorkspaceSuccessState;

interface AdminWorkspaceResponseBody {
  name: string;
  slug: string;
  timezone: string;
}

const GENERIC_NETWORK_ERROR = "Unable to reach the server. Please try again.";

function errorState(partial: Omit<SaveWorkspaceErrorState, "status">): SaveWorkspaceErrorState {
  return { status: "error", ...partial };
}

export async function saveWorkspace(
  _prevState: SaveWorkspaceState,
  formData: FormData
): Promise<SaveWorkspaceState> {
  const timezoneRaw = String(formData.get("timezone") ?? "").trim();

  const parsed = workspaceFormSchema.safeParse({
    name: String(formData.get("name") ?? ""),
    slug: String(formData.get("slug") ?? ""),
    timezone: timezoneRaw || undefined,
  });

  if (!parsed.success) {
    const fieldErrors: SaveWorkspaceFieldErrors = {};
    for (const issue of parsed.error.issues) {
      const key = issue.path[0];
      if (key === "name") fieldErrors.name ??= issue.message;
      else if (key === "slug") fieldErrors.slug ??= issue.message;
      else if (key === "timezone") fieldErrors.timezone ??= issue.message;
    }
    return errorState({
      fieldErrors,
      formError: Object.keys(fieldErrors).length === 0 ? "Check the form and try again." : null,
      correlationId: null,
    });
  }

  const requestBody = {
    name: parsed.data.name,
    slug: parsed.data.slug,
    timezone: parsed.data.timezone ?? null,
  };

  let response: Response;
  try {
    response = await adminApiFetch("/admin/workspace", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(requestBody),
    });
  } catch (err) {
    if (err instanceof AdminApiError) {
      return mapAdminApiError(err);
    }
    return errorState({ fieldErrors: {}, formError: GENERIC_NETWORK_ERROR, correlationId: null });
  }

  const body = (await response.json()) as AdminWorkspaceResponseBody;

  revalidatePath("/workspace");

  return { status: "saved", workspace: body };
}

function mapAdminApiError(err: AdminApiError): SaveWorkspaceErrorState {
  if (err.status === 403 || err.errorCode === "ROLE_NOT_PERMITTED") {
    return errorState({
      fieldErrors: {},
      formError: "You do not have permission to change workspace settings.",
      correlationId: err.correlationId || null,
    });
  }
  if (err.status === 401) {
    return errorState({
      fieldErrors: {},
      formError: "Your session has expired. Please sign in again.",
      correlationId: err.correlationId || null,
    });
  }
  if (err.errorCode === "INVALID_TIMEZONE") {
    return errorState({
      fieldErrors: { timezone: err.message || "Not a valid timezone." },
      formError: null,
      correlationId: err.correlationId || null,
    });
  }
  if (err.errorCode === "INVALID_SLUG" || err.errorCode === "WORKSPACE_SLUG_TAKEN") {
    return errorState({
      fieldErrors: { slug: err.message || "That workspace URL isn't available." },
      formError: null,
      correlationId: err.correlationId || null,
    });
  }
  if (err.status === 422) {
    return errorState({
      fieldErrors: {},
      formError: "The server rejected one or more values — check them and try again.",
      correlationId: err.correlationId || null,
    });
  }
  return errorState({
    fieldErrors: {},
    formError: `${err.message || "Something went wrong."} (correlation ID: ${
      err.correlationId || "unknown"
    })`,
    correlationId: err.correlationId || null,
  });
}

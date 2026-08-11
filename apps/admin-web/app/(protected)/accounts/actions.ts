"use server";

/**
 * Add-account server action (SR-17 scope item 6). `CLIENT_ADMIN`-only per
 * D2, enforced by the backend's `require_roles(Role.CLIENT_ADMIN)` on
 * `POST /admin/accounts` (accounts/admin_routes.py:108) -- this action does
 * not re-check role server-side, matching every other action in this
 * console (the API is the real gate).
 *
 * There is no update action in this file, deliberately -- there is no
 * `PATCH /admin/accounts/{id}` endpoint (M3/D2); building one against a
 * nonexistent route is explicitly forbidden by this sprint's scope.
 */
import { revalidatePath } from "next/cache";
import { AdminApiError, adminApiFetch } from "@/lib/api";
import { addAccountFormSchema } from "@/lib/accounts-schema";

export interface AddAccountIdleState {
  status: "idle";
}

export interface AddAccountErrorState {
  status: "error";
  message: string;
}

export interface AddAccountOkState {
  status: "ok";
  accountId: string;
}

export type AddAccountState = AddAccountIdleState | AddAccountErrorState | AddAccountOkState;

const GENERIC_NETWORK_ERROR = "Unable to reach the server. Please try again.";

export async function addAccount(
  _prevState: AddAccountState,
  formData: FormData
): Promise<AddAccountState> {
  const raw = {
    name: formData.get("name")?.toString() ?? "",
    domain: formData.get("domain")?.toString() ?? "",
  };

  const parsed = addAccountFormSchema.safeParse(raw);
  if (!parsed.success) {
    const firstIssue = parsed.error.issues[0];
    return { status: "error", message: firstIssue?.message ?? "Invalid form input." };
  }

  const { data } = parsed;

  try {
    const response = await adminApiFetch("/admin/accounts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: data.name, domain: data.domain ?? null }),
    });
    const body = (await response.json()) as { account_id: string };
    revalidatePath("/accounts");
    return { status: "ok", accountId: body.account_id };
  } catch (error) {
    if (error instanceof AdminApiError) {
      return { status: "error", message: mapError(error) };
    }
    return { status: "error", message: GENERIC_NETWORK_ERROR };
  }
}

function mapError(error: AdminApiError): string {
  if (error.status === 403 || error.errorCode === "ROLE_NOT_PERMITTED") {
    return "You do not have permission to add accounts.";
  }
  if (error.status === 401) {
    return "Your session has expired. Please log in again.";
  }
  return `${error.message} (correlation ID: ${error.correlationId || "unknown"}).`;
}

"use server";

/**
 * Delete-the-active-chatbot server action. Calls `DELETE /admin/tenants/mine`
 * (services/api/src/api/admin/routes.py) -- a hard delete of the caller's
 * OWN currently-active chatbot, which re-mints the session cookie onto
 * another chatbot in the same account in the same response (blocked with
 * 422 `LAST_CHATBOT` if this is the account's only one left).
 *
 * Same auth-bridge shape as `switch-tenant/actions.ts#switchTenantAction`:
 * read the fresh JWT out of admin-api's `Set-Cookie` via `extractAccessToken`
 * and re-set it as this app's own cookie. Unlike that action, failures are
 * returned (not redirected) -- this is invoked from a confirm dialog that
 * needs to stay open and show the real error (pessimistic UI, matches
 * `OffRampDialog`'s pattern), not bounce the caller to a query-string flag.
 */
import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import { revalidatePath } from "next/cache";
import { ACCESS_TOKEN_COOKIE, extractAccessToken, ttlSecondsFromToken } from "@/lib/auth";
import { adminApiFetch, AdminApiError } from "@/lib/api";

export interface DeleteChatbotErrorResult {
  status: "error";
  message: string;
}

function mapDeleteErrorMessage(error: AdminApiError): string {
  if (error.errorCode === "LAST_CHATBOT") {
    return "This is the only chatbot left in your account. Delete another chatbot first, or contact support.";
  }
  if (error.status === 403 || error.errorCode === "ROLE_NOT_PERMITTED") {
    return "You do not have permission to delete this chatbot.";
  }
  if (error.status === 401) {
    return "Your session has expired. Please log in again.";
  }
  return `Something went wrong (${error.errorCode || "UNKNOWN_ERROR"}). Correlation ID: ${
    error.correlationId || "n/a"
  }.`;
}

/** On success, redirects to `/` (never returns). On failure, returns an
 * error result for the dialog to render inline. */
export async function deleteChatbotAction(): Promise<DeleteChatbotErrorResult> {
  let response: Response;
  try {
    response = await adminApiFetch("/admin/tenants/mine", { method: "DELETE" });
  } catch (error) {
    if (error instanceof AdminApiError) {
      return { status: "error", message: mapDeleteErrorMessage(error) };
    }
    return { status: "error", message: "Unable to reach the server. Please try again." };
  }

  const setCookieValues =
    typeof response.headers.getSetCookie === "function"
      ? response.headers.getSetCookie()
      : (() => {
          const single = response.headers.get("set-cookie");
          return single ? [single] : [];
        })();

  const token = extractAccessToken(setCookieValues);
  if (!token) {
    return {
      status: "error",
      message:
        "The chatbot was deleted, but your session could not be refreshed. Please log in again.",
    };
  }

  const isProd = process.env.NODE_ENV === "production";
  const cookieStore = await cookies();
  cookieStore.set(ACCESS_TOKEN_COOKIE, token, {
    httpOnly: true,
    secure: isProd,
    sameSite: "lax",
    path: "/",
    maxAge: ttlSecondsFromToken(token),
  });

  revalidatePath("/", "layout");
  redirect("/");
}

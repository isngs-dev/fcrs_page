"use server";

/**
 * Switch-tenant server action (multi-chatbot accounts) -- re-mints this
 * app's own session cookie scoped to a different chatbot within the
 * caller's own account, via `POST /auth/switch-tenant`
 * (services/api/src/api/auth/routes.py).
 *
 * Same auth-bridge shape as `app/login/actions.ts#login`: admin-api's
 * `Set-Cookie` is scoped to ITS OWN origin, so we read the JWT out of it
 * server-to-server (via `adminApiFetch`, which already forwards the
 * caller's current cookie) and re-set it as this app's own cookie --
 * `extractAccessToken`/`ttlSecondsFromToken` are the exact same helpers
 * login uses, now shared via `lib/auth.ts`.
 */
import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import { revalidatePath } from "next/cache";
import { ACCESS_TOKEN_COOKIE, extractAccessToken, ttlSecondsFromToken } from "@/lib/auth";
import { adminApiFetch, AdminApiError } from "@/lib/api";

/**
 * Switches the active chatbot, then redirects to `/` (the dashboard for the
 * newly-active tenant). A rejected switch (unknown/cross-account/disabled
 * target -- 404 `TENANT_NOT_FOUND`) redirects to `/chatbots` with an honest
 * query-string flag rather than throwing into the caller's render tree,
 * since this is invoked directly from a client-side `<select>` handler, not
 * a form with its own error-state UI.
 */
export async function switchTenantAction(tenantId: string): Promise<void> {
  let response: Response;
  try {
    response = await adminApiFetch("/auth/switch-tenant", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tenant_id: tenantId }),
    });
  } catch (error) {
    if (error instanceof AdminApiError) {
      redirect(`/chatbots?switchError=${encodeURIComponent(error.errorCode || "UNKNOWN_ERROR")}`);
    }
    redirect("/chatbots?switchError=UNREACHABLE");
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
    redirect("/chatbots?switchError=NO_SESSION_ISSUED");
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

  // Every tenant-scoped page derives its data from the cookie alone (no
  // `tenantId` route param on the client-facing side) -- revalidate the
  // whole tree so none of them serve a stale render from before the switch.
  revalidatePath("/", "layout");
  redirect("/");
}

/**
 * Google Calendar OAuth redirect target (SR-22). `GOOGLE_OAUTH_REDIRECT_URI`
 * must point HERE (admin-web's origin), not at admin-api directly: the
 * admin's `access_token` cookie is set on admin-web's own origin (see
 * `app/login/actions.ts`), so a browser navigation from Google straight to
 * admin-api arrives without it and `GET /admin/schedule/calendar/google/callback`
 * answers 401 UNAUTHENTICATED. Same cookie-forwarding proxy shape as
 * `leads/export/route.ts`: forward Google's query string server-to-server
 * with the cookie attached, then hand the browser admin-api's own redirect
 * (`/workspace?calendar_connected=true` or `?calendar_error=<reason>`).
 */
import "server-only";

import { NextResponse, type NextRequest } from "next/server";
import { cookies } from "next/headers";
import { env } from "@/lib/env";
import { ACCESS_TOKEN_COOKIE } from "@/lib/auth";

export async function GET(request: NextRequest): Promise<NextResponse> {
  const cookieStore = await cookies();
  const token = cookieStore.get(ACCESS_TOKEN_COOKIE)?.value;

  const headers = new Headers();
  if (token) {
    headers.set("Cookie", `${ACCESS_TOKEN_COOKIE}=${token}`);
  }

  const failed = new URL("/workspace?calendar_error=unexpected", request.url);
  let upstream: Response;
  try {
    upstream = await fetch(
      `${env.adminApiBaseUrl}/admin/schedule/calendar/google/callback${request.nextUrl.search}`,
      // Manual: the Location is meant for the BROWSER, not this server.
      { headers, cache: "no-store", redirect: "manual" }
    );
  } catch {
    return NextResponse.redirect(failed);
  }

  const location = upstream.headers.get("Location");
  if (upstream.status >= 300 && upstream.status < 400 && location) {
    return NextResponse.redirect(location);
  }
  return NextResponse.redirect(failed);
}

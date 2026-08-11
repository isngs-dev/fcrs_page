/**
 * CSV download proxy for `/leads`'s Export CSV button (SR-22 scope item 5 /
 * M6). Mirrors `reports/csv/[report]/route.ts`'s pattern exactly: a plain
 * `<a href>` cannot point directly at admin-api's `/admin/leads/export`
 * endpoint (server-to-server calls forward the caller's `access_token`
 * cookie manually because a cross-origin browser request does not ride the
 * same cookie jar admin-api expects) -- this route re-implements that same
 * cookie-forwarding, then streams the backend's CSV bytes straight back to
 * the browser, preserving `Content-Type`/`Content-Disposition`.
 *
 * `GET /admin/leads/export` already exists and is tenant-scoped server-side
 * (`services/api/src/api/leads/admin_routes.py`, `list_leads_for_export`
 * filters by `claims.tenant_id`) -- no new backend endpoint was added for
 * this. No query params are forwarded: the backend endpoint takes none
 * (unlike the reports CSV routes, which accept `from`/`to`/filters).
 */
import "server-only";

import { NextResponse } from "next/server";
import { cookies } from "next/headers";
import { env } from "@/lib/env";
import { ACCESS_TOKEN_COOKIE } from "@/lib/auth";

export async function GET(): Promise<NextResponse> {
  const cookieStore = await cookies();
  const token = cookieStore.get(ACCESS_TOKEN_COOKIE)?.value;

  const headers = new Headers();
  if (token) {
    headers.set("Cookie", `${ACCESS_TOKEN_COOKIE}=${token}`);
  }

  const upstream = await fetch(`${env.adminApiBaseUrl}/admin/leads/export`, {
    headers,
    cache: "no-store",
  });

  if (!upstream.ok || !upstream.body) {
    let body: unknown;
    try {
      body = await upstream.json();
    } catch {
      body = { error_code: "UNKNOWN_ERROR", message: "Export failed." };
    }
    return NextResponse.json(body, { status: upstream.status || 502 });
  }

  const responseHeaders = new Headers();
  responseHeaders.set("Content-Type", upstream.headers.get("Content-Type") ?? "text/csv");
  const disposition = upstream.headers.get("Content-Disposition");
  if (disposition) responseHeaders.set("Content-Disposition", disposition);

  return new NextResponse(upstream.body, { status: 200, headers: responseHeaders });
}

/**
 * CSV download proxy for the SR-9.5 fixed reports (D8/D10, DoD item 30:
 * "Download CSV downloads a file that opens cleanly in a spreadsheet").
 *
 * A plain `<a href>` cannot point directly at admin-api's `.csv` endpoints:
 * server-to-server calls (`adminApiFetch`) forward the caller's
 * `access_token` cookie manually as a `Cookie` header (lib/api.ts) because
 * a cross-origin browser request does not ride the same cookie jar admin-api
 * expects, and `leads/page.tsx` already disclosed this exact gap rather
 * than faking a working link ("wiring a browser-safe download route is out
 * of scope for this UI-only pass"). This route closes that gap for the new
 * reports: it re-implements the same cookie-forwarding as `adminApiFetch`,
 * then streams the backend's CSV bytes straight back to the browser,
 * preserving `Content-Type` and `Content-Disposition` so the file downloads
 * with the correct name and opens cleanly in a spreadsheet.
 *
 * `report` is one of the four fixed report names (validated against a
 * closed set -- never interpolated into a path without validation). All
 * other query params (`from`, `to`, `bucket`, `source`, `status`,
 * `assigned_agent_id`, `owner_agent_id`, and an optional `tenant_id` for
 * the PLATFORM_ADMIN tenant-explicit variant) pass through verbatim to
 * admin-api, which performs the real auth/validation -- this route is a
 * thin, unauthenticated-by-itself pipe; admin-api still rejects a caller
 * without a valid cookie exactly as it does for the JSON endpoints.
 */
import "server-only";

import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";
import { cookies } from "next/headers";
import { env } from "@/lib/env";
import { ACCESS_TOKEN_COOKIE } from "@/lib/auth";

const _VALID_REPORTS = new Set([
  "leads-by-stage",
  "bookings",
  "funnel",
  "win-loss",
  // SR-19
  "lead-sources",
  "score-distribution",
  "agent-performance",
  "recent-conversions",
]);

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ report: string }> }
): Promise<NextResponse> {
  const { report } = await params;

  if (!_VALID_REPORTS.has(report)) {
    return NextResponse.json({ error_code: "UNKNOWN_REPORT", message: "Unknown report." }, { status: 404 });
  }

  const cookieStore = await cookies();
  const token = cookieStore.get(ACCESS_TOKEN_COOKIE)?.value;

  const incoming = request.nextUrl.searchParams;
  const tenantId = incoming.get("tenant_id");
  const forwarded = new URLSearchParams(incoming);
  forwarded.delete("tenant_id");

  const basePath = tenantId
    ? `/admin/tenants/${encodeURIComponent(tenantId)}/analytics/reports/${report}.csv`
    : `/admin/analytics/reports/${report}.csv`;

  const headers = new Headers();
  if (token) {
    headers.set("Cookie", `${ACCESS_TOKEN_COOKIE}=${token}`);
  }

  const upstream = await fetch(`${env.adminApiBaseUrl}${basePath}?${forwarded.toString()}`, {
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

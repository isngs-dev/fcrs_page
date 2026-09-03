/**
 * Knowledge-document download proxy (platform-admin "Export" action).
 * Mirrors `reports/csv/[report]/route.ts`'s exact pattern: a plain
 * `<a href>` cannot point directly at admin-api's endpoints (server-to-server
 * calls forward the caller's `access_token` cookie manually -- a
 * cross-origin browser request does not ride the same cookie jar admin-api
 * expects), so this route re-implements that same cookie-forwarding, then
 * streams the backend's raw file bytes straight back to the browser,
 * preserving `Content-Type` and `Content-Disposition` so the download opens
 * with the original filename.
 *
 * `docId` passes straight through to admin-api's own tenant-isolated
 * `get_doc` lookup -- this route does no authorization itself, same
 * "thin, unauthenticated-by-itself pipe" contract as the CSV proxy.
 * `tenant_id` (optional query param, not a path segment -- same convention
 * the CSV proxy uses) selects the PLATFORM_ADMIN tenant-scoped backend path.
 */
import "server-only";

import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";
import { cookies } from "next/headers";
import { env } from "@/lib/env";
import { ACCESS_TOKEN_COOKIE } from "@/lib/auth";

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ docId: string }> }
): Promise<NextResponse> {
  const { docId } = await params;

  const cookieStore = await cookies();
  const token = cookieStore.get(ACCESS_TOKEN_COOKIE)?.value;

  const tenantId = request.nextUrl.searchParams.get("tenant_id");
  const basePath = tenantId
    ? `/admin/tenants/${encodeURIComponent(tenantId)}/ingestion/docs/${encodeURIComponent(docId)}/download`
    : `/admin/ingestion/docs/${encodeURIComponent(docId)}/download`;

  const headers = new Headers();
  if (token) {
    headers.set("Cookie", `${ACCESS_TOKEN_COOKIE}=${token}`);
  }

  const upstream = await fetch(`${env.adminApiBaseUrl}${basePath}`, {
    headers,
    cache: "no-store",
  });

  if (!upstream.ok || !upstream.body) {
    let body: unknown;
    try {
      body = await upstream.json();
    } catch {
      body = { error_code: "UNKNOWN_ERROR", message: "Download failed." };
    }
    return NextResponse.json(body, { status: upstream.status || 502 });
  }

  const responseHeaders = new Headers();
  responseHeaders.set(
    "Content-Type",
    upstream.headers.get("Content-Type") ?? "application/octet-stream"
  );
  const disposition = upstream.headers.get("Content-Disposition");
  if (disposition) responseHeaders.set("Content-Disposition", disposition);

  return new NextResponse(upstream.body, { status: 200, headers: responseHeaders });
}

/**
 * Rejected-rows CSV download for an import preview (A8 U9) -- the same thin
 * cookie-forwarding pipe as `knowledge/download/[docId]/route.ts`.
 */
import "server-only";

import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";
import { cookies } from "next/headers";
import { env } from "@/lib/env";
import { ACCESS_TOKEN_COOKIE } from "@/lib/auth";

export async function GET(
  _request: NextRequest,
  { params }: { params: Promise<{ importId: string }> }
): Promise<NextResponse> {
  const { importId } = await params;
  const token = (await cookies()).get(ACCESS_TOKEN_COOKIE)?.value;
  const headers = new Headers();
  if (token) headers.set("Cookie", `${ACCESS_TOKEN_COOKIE}=${token}`);

  const upstream = await fetch(
    `${env.adminApiBaseUrl}/admin/outreach/imports/${encodeURIComponent(importId)}/rejected.csv`,
    { headers, cache: "no-store" }
  );
  if (!upstream.ok || !upstream.body) {
    return NextResponse.json({ error_code: "IMPORT_NOT_FOUND" }, { status: upstream.status || 502 });
  }
  return new NextResponse(upstream.body, {
    status: 200,
    headers: {
      "Content-Type": "text/csv",
      "Content-Disposition": upstream.headers.get("Content-Disposition") ?? 'attachment; filename="rejected.csv"',
    },
  });
}

/**
 * Call-recording proxy (A8 U6), same thin cookie-forwarding pipe as
 * `knowledge/download/[docId]/route.ts`: the backend does the tenant + role
 * checks and fetches the mp3 from Plivo; this only streams it to the <audio>.
 */
import "server-only";

import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";
import { cookies } from "next/headers";
import { env } from "@/lib/env";
import { ACCESS_TOKEN_COOKIE } from "@/lib/auth";

export async function GET(
  _request: NextRequest,
  { params }: { params: Promise<{ callId: string }> }
): Promise<NextResponse> {
  const { callId } = await params;
  const token = (await cookies()).get(ACCESS_TOKEN_COOKIE)?.value;
  const headers = new Headers();
  if (token) headers.set("Cookie", `${ACCESS_TOKEN_COOKIE}=${token}`);

  const upstream = await fetch(
    `${env.adminApiBaseUrl}/admin/outreach/calls/${encodeURIComponent(callId)}/recording`,
    { headers, cache: "no-store" }
  );
  if (!upstream.ok || !upstream.body) {
    return NextResponse.json({ error_code: "RECORDING_NOT_FOUND" }, { status: upstream.status || 502 });
  }
  return new NextResponse(upstream.body, {
    status: 200,
    headers: { "Content-Type": "audio/mpeg", "Cache-Control": "no-store" },
  });
}

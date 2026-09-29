import { afterEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";

const cookieGetMock = vi.fn();

vi.mock("next/headers", () => ({
  cookies: vi.fn(async () => ({ get: cookieGetMock })),
}));

const { GET } = await import("@/app/(protected)/workspace/google-calendar/callback/route");
const { ACCESS_TOKEN_COOKIE } = await import("@/lib/auth");

function buildRequest(search: string): NextRequest {
  return new NextRequest(
    new URL(`/workspace/google-calendar/callback${search}`, "http://localhost:3000")
  );
}

describe("GET /workspace/google-calendar/callback", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    cookieGetMock.mockReset();
  });

  it("forwards code/state + the session cookie and relays admin-api's redirect", async () => {
    cookieGetMock.mockReturnValue({ value: "jwt.abc" });
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(null, {
        status: 307,
        headers: { Location: "http://localhost:3000/workspace?calendar_connected=true" },
      })
    );
    vi.stubGlobal("fetch", fetchMock);

    const resp = await GET(buildRequest("?code=c1&state=s1"));

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(
      "http://localhost:8000/admin/schedule/calendar/google/callback?code=c1&state=s1"
    );
    expect(init.redirect).toBe("manual");
    expect((init.headers as Headers).get("Cookie")).toBe(`${ACCESS_TOKEN_COOKIE}=jwt.abc`);
    expect(resp.headers.get("Location")).toBe(
      "http://localhost:3000/workspace?calendar_connected=true"
    );
  });

  it("lands on the workspace error banner when admin-api answers without a redirect", async () => {
    cookieGetMock.mockReturnValue({ value: "jwt.abc" });
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ error_code: "UNAUTHENTICATED" }), { status: 401 })
      )
    );

    const resp = await GET(buildRequest("?code=c1&state=s1"));

    expect(resp.headers.get("Location")).toBe(
      "http://localhost:3000/workspace?calendar_error=unexpected"
    );
  });
});

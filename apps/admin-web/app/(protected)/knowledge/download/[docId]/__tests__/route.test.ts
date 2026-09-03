/**
 * Knowledge-document download proxy route -- mirrors
 * `reports/csv/[report]/route.ts`'s own cookie-forwarding/streaming
 * contract (see that route's header comment); this is the first test
 * coverage for either proxy shape, written fresh for the new route.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";

const cookieGetMock = vi.fn();

vi.mock("next/headers", () => ({
  cookies: vi.fn(async () => ({ get: cookieGetMock })),
}));

const { GET } = await import("@/app/(protected)/knowledge/download/[docId]/route");
const { ACCESS_TOKEN_COOKIE } = await import("@/lib/auth");

function buildRequest(url: string): NextRequest {
  return new NextRequest(new URL(url, "http://localhost:3000"));
}

describe("GET /knowledge/download/[docId]", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    cookieGetMock.mockReset();
  });

  it("forwards the access_token cookie and streams the implicit (own-tenant) backend path when tenant_id is absent", async () => {
    cookieGetMock.mockReturnValue({ value: "jwt.abc" });
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(new Blob([new Uint8Array([1, 2, 3])]).stream(), {
        status: 200,
        headers: {
          "Content-Type": "text/plain",
          "Content-Disposition": 'attachment; filename="sample.txt"',
        },
      })
    );
    vi.stubGlobal("fetch", fetchMock);

    const resp = await GET(buildRequest("/knowledge/download/doc-1"), {
      params: Promise.resolve({ docId: "doc-1" }),
    });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://localhost:8000/admin/ingestion/docs/doc-1/download");
    expect((init.headers as Headers).get("Cookie")).toBe(`${ACCESS_TOKEN_COOKIE}=jwt.abc`);

    expect(resp.status).toBe(200);
    expect(resp.headers.get("Content-Type")).toBe("text/plain");
    expect(resp.headers.get("Content-Disposition")).toBe('attachment; filename="sample.txt"');
  });

  it("maps a tenant_id query param to the PLATFORM_ADMIN tenant-scoped backend path", async () => {
    cookieGetMock.mockReturnValue({ value: "jwt.abc" });
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(new Blob([new Uint8Array([1])]).stream(), { status: 200 })
    );
    vi.stubGlobal("fetch", fetchMock);

    await GET(buildRequest("/knowledge/download/doc-1?tenant_id=tenant-42"), {
      params: Promise.resolve({ docId: "doc-1" }),
    });

    const [url] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(
      "http://localhost:8000/admin/tenants/tenant-42/ingestion/docs/doc-1/download"
    );
  });

  it("passes through an honest upstream error body/status instead of fabricating a file", async () => {
    cookieGetMock.mockReturnValue({ value: "jwt.abc" });
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ error_code: "DOC_NOT_FOUND", message: "Not found." }), {
        status: 404,
      })
    );
    vi.stubGlobal("fetch", fetchMock);

    const resp = await GET(buildRequest("/knowledge/download/does-not-exist"), {
      params: Promise.resolve({ docId: "does-not-exist" }),
    });

    expect(resp.status).toBe(404);
    const body = (await resp.json()) as { error_code: string };
    expect(body.error_code).toBe("DOC_NOT_FOUND");
  });

  it("does not send a Cookie header at all when there is no access_token cookie", async () => {
    cookieGetMock.mockReturnValue(undefined);
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(new Blob([new Uint8Array([1])]).stream(), { status: 200 })
    );
    vi.stubGlobal("fetch", fetchMock);

    await GET(buildRequest("/knowledge/download/doc-1"), {
      params: Promise.resolve({ docId: "doc-1" }),
    });

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect((init.headers as Headers).has("Cookie")).toBe(false);
  });
});

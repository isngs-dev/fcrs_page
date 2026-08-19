import { describe, expect, it } from "vitest";
import jwt from "jsonwebtoken";
import { NextRequest } from "next/server";
// NOTE: the installed Next.js version (16.2.10) still exports this testing
// helper under its pre-rename name `unstable_doesMiddlewareMatch` even
// though the file-convention itself is `proxy.ts`/`proxy()` -- verified by
// inspecting the actual module exports (docs describe a newer name that
// isn't present in this version). Using the real export, not the doc name.
import { unstable_doesMiddlewareMatch as unstable_doesProxyMatch } from "next/experimental/testing/server";
import { proxy, config } from "@/proxy";

const SECRET = process.env.JWT_SECRET as string;

function requestTo(path: string, cookieValue?: string): NextRequest {
  const req = new NextRequest(new URL(path, "http://localhost:3000"));
  if (cookieValue !== undefined) {
    req.cookies.set("access_token", cookieValue);
  }
  return req;
}

const validToken = jwt.sign(
  { sub: "u1", role: "CLIENT_ADMIN", tenant_id: "t1", project_ids: [] },
  SECRET,
  { algorithm: "HS256", expiresIn: "1h" }
);

const platformAdminToken = jwt.sign(
  { sub: "p1", role: "PLATFORM_ADMIN", tenant_id: null, project_ids: [] },
  SECRET,
  { algorithm: "HS256", expiresIn: "1h" }
);

const clientAgentToken = jwt.sign(
  { sub: "a1", role: "CLIENT_AGENT", tenant_id: "t1", project_ids: [] },
  SECRET,
  { algorithm: "HS256", expiresIn: "1h" }
);

describe("proxy (route gate)", () => {
  it("redirects to /login when no cookie is present on a protected route", () => {
    const res = proxy(requestTo("/"));
    expect(res.status).toBe(307);
    expect(res.headers.get("location")).toContain("/login");
  });

  it("redirects to /login when the cookie is invalid/tampered", () => {
    const res = proxy(requestTo("/", "not-a-valid-jwt"));
    expect(res.status).toBe(307);
    expect(res.headers.get("location")).toContain("/login");
  });

  it("passes through when the cookie holds a valid token", () => {
    const res = proxy(requestTo("/", validToken));
    // NextResponse.next() has no redirect status/location.
    expect(res.headers.get("location")).toBeNull();
  });

  it("matcher config excludes /login (never gated, no redirect loop)", () => {
    expect(
      unstable_doesProxyMatch({ config, nextConfig: {}, url: "/login" })
    ).toBe(false);
  });

  it("matcher config excludes /forgot-password and /reset-password (logged-out visitors must reach them)", () => {
    expect(
      unstable_doesProxyMatch({ config, nextConfig: {}, url: "/forgot-password" })
    ).toBe(false);
    expect(
      unstable_doesProxyMatch({ config, nextConfig: {}, url: "/reset-password" })
    ).toBe(false);
  });

  it("matcher config includes protected paths", () => {
    expect(unstable_doesProxyMatch({ config, nextConfig: {}, url: "/" })).toBe(
      true
    );
    expect(
      unstable_doesProxyMatch({ config, nextConfig: {}, url: "/leads" })
    ).toBe(true);
  });

  // S13.7 D4/D6 -- role-aware routing to/from the client list. Defense-in-
  // depth UI only; the backend still enforces regardless.
  it("redirects a PLATFORM_ADMIN request to / to /clients (D4: no single-tenant dashboard of their own)", () => {
    const res = proxy(requestTo("/", platformAdminToken));
    expect(res.status).toBe(307);
    expect(res.headers.get("location")).toContain("/clients");
  });

  it("passes through a PLATFORM_ADMIN request to /clients/{tenantId}/settings", () => {
    const res = proxy(requestTo("/clients/tenant-x/settings", platformAdminToken));
    expect(res.headers.get("location")).toBeNull();
  });

  it("passes through a PLATFORM_ADMIN request to /clients", () => {
    const res = proxy(requestTo("/clients", platformAdminToken));
    expect(res.headers.get("location")).toBeNull();
  });

  it("redirects a CLIENT_ADMIN request to /clients to their own dashboard (D6: never sees the client list)", () => {
    const res = proxy(requestTo("/clients", validToken));
    expect(res.status).toBe(307);
    const location = res.headers.get("location");
    expect(location).not.toBeNull();
    expect(new URL(location as string).pathname).toBe("/");
  });

  it("redirects a CLIENT_AGENT request to /clients/{tenantId}/settings to their own dashboard", () => {
    const res = proxy(requestTo("/clients/tenant-x/settings", clientAgentToken));
    expect(res.status).toBe(307);
    const location = res.headers.get("location");
    expect(location).not.toBeNull();
    expect(new URL(location as string).pathname).toBe("/");
  });

  it("a CLIENT_ADMIN's own feature route still renders unchanged (S13.2-S13.6 untouched)", () => {
    const res = proxy(requestTo("/leads", validToken));
    expect(res.headers.get("location")).toBeNull();
  });

  it("an unauthenticated request to /clients still redirects to /login (S13.1 behavior preserved)", () => {
    const res = proxy(requestTo("/clients"));
    expect(res.status).toBe(307);
    expect(res.headers.get("location")).toContain("/login");
  });
});

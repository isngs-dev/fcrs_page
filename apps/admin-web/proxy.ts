/**
 * Route gate (S13.1 decisions 2 & 3; extended by S13.7 D4/D6).
 *
 * NAMING NOTE: the S13.1 spec calls this file `middleware.ts`. The
 * installed Next.js version (16.2.10) deprecated that file convention in
 * favor of `proxy.ts` (exported function `proxy`, not `middleware`) --
 * `middleware.ts` still works but is deprecated and locked to the Edge
 * runtime, whereas `proxy.ts` defaults to the Node.js runtime, which is
 * what we want here anyway (jsonwebtoken needs Node's `crypto`, not an
 * Edge-compatible JWT library). This is a mechanical rename forced by the
 * framework version, not a deviation from decisions 1/2/3's actual design:
 * same local HS256 verification, same no-network-call gate, same matcher.
 *
 * Decodes the `access_token` cookie locally (shared `verifyToken` from
 * lib/auth.ts -- single source of truth, not duplicated here) rather than
 * calling `/auth/me` on every request, per decision 2. This is a fast
 * pre-render gate for unauthenticated access only; it is NOT the
 * fine-grained per-screen authorization boundary (the backend remains
 * that boundary, per CLAUDE.md/admin-web skill: "the API is the real
 * boundary").
 *
 * S13.7 D4/D6 (defense-in-depth UI routing only -- the backend still
 * enforces regardless of what this gate does):
 * - A PLATFORM_ADMIN landing on the single-tenant home (`/`) is redirected
 *   to the client list (`/clients`) -- they have no single-tenant dashboard
 *   of their own (D4).
 * - A CLIENT_ADMIN/CLIENT_AGENT forcing `/clients` (or any `/clients/**`)
 *   is redirected to their own dashboard (`/`) -- they never see the client
 *   list (D6). The per-screen `requireRole("PLATFORM_ADMIN")` calls in
 *   `clients/page.tsx` and `clients/[tenantId]/layout.tsx` are the same
 *   belt-and-suspenders pattern `lib/auth.ts`'s `requireRole` docstring
 *   already establishes for `/tenants/new` and `/knowledge`.
 */
import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";
import { ACCESS_TOKEN_COOKIE, verifyToken } from "@/lib/auth";

export function proxy(request: NextRequest) {
  const token = request.cookies.get(ACCESS_TOKEN_COOKIE)?.value;
  const claims = verifyToken(token);

  if (!claims) {
    const loginUrl = new URL("/login", request.url);
    return NextResponse.redirect(loginUrl);
  }

  const { pathname } = request.nextUrl;

  if (claims.role === "PLATFORM_ADMIN" && pathname === "/") {
    return NextResponse.redirect(new URL("/clients", request.url));
  }

  if (claims.role !== "PLATFORM_ADMIN" && pathname.startsWith("/clients")) {
    return NextResponse.redirect(new URL("/", request.url));
  }

  return NextResponse.next();
}

export const config = {
  // Everything except the login page, the password-reset request/confirm
  // pages, and Next.js internals requires a valid cookie -- those three are
  // the only screens a logged-out visitor must reach (no redirect loop).
  matcher: ["/((?!login|forgot-password|reset-password|_next|favicon.ico).*)"],
};

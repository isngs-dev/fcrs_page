/**
 * Server-only JWT decode/verify for the `access_token` cookie admin-api
 * mints. Single source of truth for claim decoding -- shared by `proxy.ts`
 * (route gate) and server components/actions (defense-in-depth + display).
 *
 * Mirrors the backend's token shape exactly (services/api/src/api/auth/
 * tokens.py `create_access_token`): HS256, claims `{sub, role, tenant_id,
 * project_ids, iat, exp, jti}`. We verify with the same HS256 secret the
 * backend used to sign (`JWT_SECRET` env var, must match admin-api's
 * `jwt_secret`) -- we never mint tokens ourselves, only verify.
 */
import "server-only";

import jwt from "jsonwebtoken";
import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import { env } from "@/lib/env";

export const ACCESS_TOKEN_COOKIE = "access_token";

/** The four platform roles (mirrors common.auth.Role on the backend). */
export type Role =
  | "PLATFORM_ADMIN"
  | "CLIENT_ADMIN"
  | "CLIENT_AGENT"
  | "VISITOR";

export interface Claims {
  subject: string;
  role: Role;
  tenantId: string | null;
  projectIds: string[];
}

interface AccessTokenPayload extends jwt.JwtPayload {
  sub: string;
  role: string;
  tenant_id: string | null;
  project_ids?: string[];
}

/**
 * Decode + verify a raw JWT string. Returns `null` on any failure (missing,
 * expired, tampered signature, malformed, unexpected shape) -- never
 * throws. Callers (proxy.ts, server components) treat `null` uniformly as
 * "not authenticated."
 */
export function verifyToken(token: string | undefined | null): Claims | null {
  if (!token) return null;

  let payload: AccessTokenPayload;
  try {
    const decoded = jwt.verify(token, env.jwtSecret, { algorithms: ["HS256"] });
    if (typeof decoded !== "object" || decoded === null) return null;
    payload = decoded as AccessTokenPayload;
  } catch {
    // Covers TokenExpiredError, JsonWebTokenError (bad signature/malformed),
    // NotBeforeError -- all treated as "not authenticated."
    return null;
  }

  if (typeof payload.sub !== "string" || typeof payload.role !== "string") {
    return null;
  }

  return {
    subject: payload.sub,
    role: payload.role as Role,
    tenantId: payload.tenant_id ?? null,
    projectIds: Array.isArray(payload.project_ids) ? payload.project_ids : [],
  };
}

/**
 * Read the caller's own `access_token` cookie (set by the login server
 * action, decision 1) and return decoded claims, or `null` if absent/
 * invalid. This is the fast, no-network local decode (decision 2) -- for
 * the authoritative live check, call `GET /auth/me` via `adminApiFetch`
 * instead.
 */
export async function getClaims(): Promise<Claims | null> {
  const cookieStore = await cookies();
  const token = cookieStore.get(ACCESS_TOKEN_COOKIE)?.value;
  return verifyToken(token);
}

/**
 * Page-level role gate (S13.2 decision 1). Server components that must be
 * restricted to a single role (e.g. `tenants/new`, PLATFORM_ADMIN-only) call
 * this instead of hand-rolling a `getClaims()` + role comparison. This is UI
 * defense-in-depth only -- the backend's own `require_roles(...)` dependency
 * is the real authorization boundary and still returns 401/403 regardless of
 * what the UI renders (admin-web skill: "the API is the real boundary").
 *
 * - No claims at all (not authenticated) -> redirect to `/login`, matching
 *   `proxy.ts`'s and `(protected)/layout.tsx`'s existing behavior.
 * - Authenticated but wrong role -> redirect to `/` (the home shell), not an
 *   error page -- this deliberately does not extend `proxy.ts` with a
 *   route->role map (decision 1: a per-tenant-settings screen later would
 *   need a different role rule on a sibling route, so the check belongs
 *   colocated with the screen it guards, not centralized).
 * - Correct role -> returns the claims for the caller to use.
 */
export async function requireRole(role: Role): Promise<Claims> {
  const claims = await getClaims();
  if (!claims) {
    redirect("/login");
  }
  if (claims.role !== role) {
    redirect("/");
  }
  return claims;
}

/**
 * Page-level MULTI-role gate (S13.4 decision 2). Mirrors `requireRole`
 * exactly but admits any role in `roles` -- for surfaces the backend allows
 * more than one role to reach (e.g. `GET /admin/leads`'s
 * `require_roles(Role.CLIENT_ADMIN, Role.CLIENT_AGENT)`, an exact allowlist
 * that intentionally excludes PLATFORM_ADMIN and VISITOR). Added as a
 * strict addition alongside `requireRole` rather than generalizing it, so
 * existing single-role call sites are untouched.
 *
 * - No claims at all (not authenticated) -> redirect to `/login`.
 * - Authenticated but role not in `roles` -> redirect to `/` (the home
 *   shell), not an error page -- same UI defense-in-depth posture as
 *   `requireRole`; the backend's own `require_roles(...)` dependency remains
 *   the real authorization boundary.
 * - Role in `roles` -> returns the claims for the caller to use.
 */
export async function requireAnyRole(...roles: Role[]): Promise<Claims> {
  const claims = await getClaims();
  if (!claims) {
    redirect("/login");
  }
  if (!roles.includes(claims.role)) {
    redirect("/");
  }
  return claims;
}

/** Default cookie lifetime fallback, mirrors admin-api's default
 * `access_token_ttl_seconds` (services/api/src/api/config.py) in case a
 * token is somehow missing/has an unreadable `exp` claim. */
const DEFAULT_TTL_SECONDS = 3600;

/**
 * Compute the remaining seconds until a freshly-minted token's `exp` claim,
 * for use as the re-issued cookie's `maxAge` (S13.1 decision 1: "matching
 * `access_token_ttl_seconds` as `maxAge`"). Falls back to
 * `DEFAULT_TTL_SECONDS` if the token doesn't verify or has no readable
 * `exp` (should not happen for a token admin-api just minted, but this
 * keeps login from hard-failing on an edge case).
 */
/**
 * Extract the JWT value from a `Set-Cookie` header emitted by admin-api for
 * `settings.cookie_name` ("access_token"). Returns `null` if not present or
 * unparseable. Shared by the login server action and the switch-tenant
 * action (multi-chatbot accounts) -- both re-mint this app's own cookie from
 * an admin-api `Set-Cookie` response the same way.
 */
export function extractAccessToken(setCookieValues: string[]): string | null {
  for (const raw of setCookieValues) {
    // A single Set-Cookie header line: "access_token=<jwt>; HttpOnly; Path=/; ..."
    const firstSegment = raw.split(";")[0]?.trim() ?? "";
    const eq = firstSegment.indexOf("=");
    if (eq === -1) continue;
    const name = firstSegment.slice(0, eq);
    const value = firstSegment.slice(eq + 1);
    if (name === ACCESS_TOKEN_COOKIE && value.length > 0) {
      return value;
    }
  }
  return null;
}

export function ttlSecondsFromToken(token: string): number {
  try {
    const decoded = jwt.verify(token, env.jwtSecret, { algorithms: ["HS256"] });
    if (typeof decoded === "object" && decoded !== null && typeof decoded.exp === "number") {
      const remaining = decoded.exp - Math.floor(Date.now() / 1000);
      if (remaining > 0) return remaining;
    }
  } catch {
    // fall through to default
  }
  return DEFAULT_TTL_SECONDS;
}

"use server";

/**
 * Login server action -- implements S13.1 decision 1 (the auth bridge).
 *
 * admin-api's POST /auth/login sets an httpOnly cookie scoped to ITS OWN
 * origin. Because this Next.js app runs on a different origin, a browser
 * would never forward that cookie to server-to-server calls we make later.
 * So: call admin-api directly from THIS server (not the browser), read the
 * raw JWT out of the `Set-Cookie` response header, and re-set it as our own
 * httpOnly cookie on this app's origin (same name, `access_token`). The
 * token is never sent to the browser as anything other than an opaque
 * httpOnly cookie value -- it never touches client-side JS.
 */
import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import { z } from "zod";
import { env } from "@/lib/env";
import { ACCESS_TOKEN_COOKIE, extractAccessToken, ttlSecondsFromToken } from "@/lib/auth";
import { PROFILE_COOKIE } from "@/lib/profile";

interface LoginProfileBody {
  id: string;
  email: string;
  role: string;
  tenant_id: string | null;
  name: string | null;
}

const loginSchema = z.object({
  email: z.string().trim().min(1, "Email is required.").email("Enter a valid email."),
  password: z.string().min(1, "Password is required."),
});

export interface LoginState {
  error: string | null;
}

/** Matches admin-api's enumeration-safe message verbatim (auth/routes.py). */
const GENERIC_AUTH_ERROR = "Invalid email or password.";

export async function login(
  _prevState: LoginState,
  formData: FormData
): Promise<LoginState> {
  const parsed = loginSchema.safeParse({
    email: formData.get("email"),
    password: formData.get("password"),
  });

  if (!parsed.success) {
    // Never leak which field was wrong -- matches backend's enumeration-safe
    // design (S13.1 scope item 5).
    return { error: GENERIC_AUTH_ERROR };
  }

  let response: Response;
  try {
    response = await fetch(`${env.adminApiBaseUrl}/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(parsed.data),
      cache: "no-store",
    });
  } catch {
    return { error: "Unable to reach the server. Please try again." };
  }

  if (!response.ok) {
    // admin-api returns a generic 401 for every auth failure mode
    // (unknown email, wrong password, inactive user) -- surface its own
    // message when present, otherwise fall back to the generic one.
    let message = GENERIC_AUTH_ERROR;
    try {
      const body = (await response.json()) as { message?: string };
      if (body.message) message = body.message;
    } catch {
      // ignore -- use fallback
    }
    return { error: message };
  }

  const setCookieValues =
    typeof response.headers.getSetCookie === "function"
      ? response.headers.getSetCookie()
      : (() => {
          const single = response.headers.get("set-cookie");
          return single ? [single] : [];
        })();

  const token = extractAccessToken(setCookieValues);
  if (!token) {
    return {
      error: "Login succeeded but no session was issued. Please try again.",
    };
  }

  // Capture email/name for display (see lib/profile.ts -- neither the JWT
  // nor GET /auth/me carries email; this is the only response that does).
  let profile: LoginProfileBody | null = null;
  try {
    profile = (await response.json()) as LoginProfileBody;
  } catch {
    profile = null;
  }

  const isProd = process.env.NODE_ENV === "production";
  const ttl = ttlSecondsFromToken(token);
  const cookieStore = await cookies();
  cookieStore.set(ACCESS_TOKEN_COOKIE, token, {
    httpOnly: true,
    secure: isProd,
    sameSite: "lax",
    path: "/",
    // Mirrors admin-api's access_token_ttl_seconds by reading the token's
    // own `exp` claim (S13.1 decision 1).
    maxAge: ttl,
  });
  if (profile) {
    cookieStore.set(
      PROFILE_COOKIE,
      JSON.stringify({ email: profile.email, name: profile.name }),
      {
        httpOnly: true,
        secure: isProd,
        sameSite: "lax",
        path: "/",
        maxAge: ttl,
      }
    );
  }

  redirect("/");
}

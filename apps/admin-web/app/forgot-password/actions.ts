"use server";

/**
 * Forgot-password request action. Calls admin-api's
 * `POST /auth/password-reset/request` directly (same "call from the server,
 * never the browser" posture as `login/actions.ts`) -- that endpoint always
 * returns 200 with an identical body for an unknown/inactive/existing email
 * (enumeration-safety, `auth/routes.py`), so this action shows the exact
 * same generic message regardless of whether the account actually exists.
 * Only a genuine infra-level failure (network error, non-2xx) surfaces a
 * different message -- mirroring `login`'s own network-failure handling.
 */
import { z } from "zod";
import { env } from "@/lib/env";

const requestSchema = z.object({
  email: z.string().trim().min(1, "Email is required.").email("Enter a valid email."),
});

export interface ForgotPasswordState {
  status: "idle" | "submitted" | "error";
  message: string | null;
}

const GENERIC_SUBMITTED_MESSAGE =
  "If that email is registered, a password reset link has been sent.";

export async function requestPasswordReset(
  _prevState: ForgotPasswordState,
  formData: FormData
): Promise<ForgotPasswordState> {
  const parsed = requestSchema.safeParse({ email: formData.get("email") });
  if (!parsed.success) {
    return {
      status: "error",
      message: parsed.error.issues[0]?.message ?? "Enter a valid email.",
    };
  }

  let response: Response;
  try {
    response = await fetch(`${env.adminApiBaseUrl}/auth/password-reset/request`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(parsed.data),
      cache: "no-store",
    });
  } catch {
    return { status: "error", message: "Unable to reach the server. Please try again." };
  }

  if (!response.ok) {
    // The backend only returns non-2xx here for infra-level failures (e.g.
    // rate limiting) -- never for "email not found" (enumeration-safety).
    return { status: "error", message: "Something went wrong. Please try again." };
  }

  return { status: "submitted", message: GENERIC_SUBMITTED_MESSAGE };
}

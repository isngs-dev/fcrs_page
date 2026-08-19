"use server";

/**
 * Reset-password confirm action. Calls admin-api's
 * `POST /auth/password-reset/confirm` directly (same server-side-only
 * posture as `login/actions.ts`) with the token from the emailed link
 * (`settings.password_reset_url_base?token=...`, `auth/routes.py`) and the
 * new password. The token is single-use (Redis GETDEL) -- a stale/reused/
 * expired token surfaces the backend's own "Invalid or expired reset
 * token." message rather than a generic one, since there's no enumeration
 * concern at this step (the visitor already proved control of the email).
 */
import { z } from "zod";
import { env } from "@/lib/env";

const confirmSchema = z.object({
  token: z.string().min(1, "Missing reset token."),
  newPassword: z.string().min(12, "Password must be at least 12 characters."),
});

export interface ResetPasswordState {
  status: "idle" | "success" | "error";
  message: string | null;
}

export async function confirmPasswordReset(
  _prevState: ResetPasswordState,
  formData: FormData
): Promise<ResetPasswordState> {
  const newPassword = formData.get("newPassword");
  const confirmPassword = formData.get("confirmPassword");
  if (typeof newPassword === "string" && newPassword !== confirmPassword) {
    return { status: "error", message: "Passwords do not match." };
  }

  const parsed = confirmSchema.safeParse({
    token: formData.get("token"),
    newPassword,
  });
  if (!parsed.success) {
    return {
      status: "error",
      message: parsed.error.issues[0]?.message ?? "Invalid input.",
    };
  }

  let response: Response;
  try {
    response = await fetch(`${env.adminApiBaseUrl}/auth/password-reset/confirm`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        token: parsed.data.token,
        new_password: parsed.data.newPassword,
      }),
      cache: "no-store",
    });
  } catch {
    return { status: "error", message: "Unable to reach the server. Please try again." };
  }

  if (!response.ok) {
    let message = "That reset link is invalid or has expired. Please request a new one.";
    try {
      const body = (await response.json()) as { message?: string };
      if (body.message) message = body.message;
    } catch {
      // ignore -- use fallback
    }
    return { status: "error", message };
  }

  return {
    status: "success",
    message: "Your password has been reset. You can now sign in.",
  };
}

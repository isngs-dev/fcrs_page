/**
 * Server functions for missed-call text-back config -- `"use server"`
 * (not `"server-only"`) because, unlike `lib/settings.ts`, BOTH functions
 * here are called directly from the self-contained client component
 * (`missed-call-config.tsx`) as well as from the settings page's server
 * component, mirroring `app/(protected)/knowledge/actions.ts`'s one-file
 * pattern (training's simpler config/mutation shape) rather than
 * `lib/settings.ts` + a separate `actions.ts`'s two-file split -- this form
 * has only three plain fields, none of the business-hours-JSON/Zod-schema
 * complexity the bot-settings form has, so a full `useActionState`+FormData
 * pipeline would be more machinery than the shape warrants.
 */
"use server";

import { adminApiFetch, AdminApiError } from "@/lib/api";

export interface CallConfig {
  monitoredPhoneNumber: string | null;
  enabled: boolean;
  textBackMessage: string | null;
}

interface CallConfigResponseBody {
  monitored_phone_number: string | null;
  enabled: boolean;
  text_back_message: string | null;
}

export type CallConfigResult =
  | { status: "ok"; config: CallConfig }
  | { status: "error"; message: string; correlationId: string };

function toCallConfig(body: CallConfigResponseBody): CallConfig {
  return {
    monitoredPhoneNumber: body.monitored_phone_number,
    enabled: body.enabled,
    textBackMessage: body.text_back_message,
  };
}

function mapErrorMessage(error: AdminApiError): string {
  if (error.status === 403 || error.errorCode === "ROLE_NOT_PERMITTED") {
    return "You do not have permission to view this setting.";
  }
  if (error.status === 401) {
    return "Your session has expired. Please log in again.";
  }
  return `Something went wrong (${error.errorCode || "UNKNOWN_ERROR"}). Correlation ID: ${
    error.correlationId || "n/a"
  }.`;
}

/**
 * Fetch the caller's tenant missed-call text-back config. Never sends
 * `tenant_id` in the body -- scoping is entirely the backend's
 * repository-layer job from the caller's own claims.
 *
 * `tenantId`: when provided, targets the PLATFORM_ADMIN super-user surface
 * `GET /admin/tenants/{tenantId}/calls/config` instead of the implicit
 * `GET /admin/calls/config`, mirroring `getBotSettings`'s exact convention.
 */
export async function getCallConfig(tenantId?: string): Promise<CallConfigResult> {
  try {
    const path = tenantId
      ? `/admin/tenants/${encodeURIComponent(tenantId)}/calls/config`
      : "/admin/calls/config";
    const response = await adminApiFetch(path);
    const body = (await response.json()) as CallConfigResponseBody;
    return { status: "ok", config: toCallConfig(body) };
  } catch (error) {
    if (error instanceof AdminApiError) {
      return { status: "error", message: mapErrorMessage(error), correlationId: error.correlationId };
    }
    return { status: "error", message: "Unable to reach the server. Please try again.", correlationId: "" };
  }
}

export type SaveCallConfigResult =
  | { status: "ok"; config: CallConfig }
  | { status: "error"; message: string; correlationId: string };

/**
 * Save the caller's tenant missed-call text-back config
 * (`PUT /admin/calls/config`, CLIENT_ADMIN only on the backend). Returns
 * the PUT response body (confirmed, not optimistic), same contract as
 * `saveSettings`.
 */
export async function saveCallConfig(
  monitoredPhoneNumber: string,
  enabled: boolean,
  textBackMessage: string,
  tenantId?: string
): Promise<SaveCallConfigResult> {
  const path = tenantId
    ? `/admin/tenants/${encodeURIComponent(tenantId)}/calls/config`
    : "/admin/calls/config";

  try {
    const response = await adminApiFetch(path, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        monitored_phone_number: monitoredPhoneNumber,
        enabled,
        text_back_message: textBackMessage,
      }),
    });
    const body = (await response.json()) as CallConfigResponseBody;
    return { status: "ok", config: toCallConfig(body) };
  } catch (error) {
    if (error instanceof AdminApiError) {
      return { status: "error", message: mapErrorMessage(error), correlationId: error.correlationId };
    }
    return { status: "error", message: "Unable to reach the server. Please try again.", correlationId: "" };
  }
}

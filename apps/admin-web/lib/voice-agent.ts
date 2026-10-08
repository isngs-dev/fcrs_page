/**
 * Server functions for the AI voice agent ("Call Us" in the widget) --
 * `"use server"`, same shape as `lib/voice-calls.ts`.
 *
 * - `GET/PUT /admin/voice-agent/config` (CLIENT_ADMIN), with the
 *   `/admin/tenants/{tenantId}/voice-agent/config` PLATFORM_ADMIN twin.
 */
"use server";

import { adminApiFetch, AdminApiError } from "@/lib/api";

export interface VoiceAgentConfig {
  enabled: boolean;
  /** `null` -> the built-in greeting. */
  greeting: string | null;
  transferNumber: string;
  timezone: string;
  /** 24-hour HH:MM in `timezone`. */
  openTime: string;
  closeTime: string;
  /** 0 = Monday ... 6 = Sunday. */
  openDays: number[];
  maxMinutes: number;
  /** Platform Plivo set up for browser calls -- "Call Us" stays hidden without it. */
  plivoReady: boolean;
}

interface VoiceAgentConfigBody {
  enabled: boolean;
  greeting: string | null;
  transfer_number: string;
  timezone: string;
  open_time: string;
  close_time: string;
  open_days: number[];
  max_minutes: number;
  plivo_ready: boolean;
}

export type VoiceAgentConfigResult =
  | { status: "ok"; config: VoiceAgentConfig }
  | { status: "error"; message: string; correlationId: string };

function path(tenantId?: string): string {
  return tenantId
    ? `/admin/tenants/${encodeURIComponent(tenantId)}/voice-agent/config`
    : "/admin/voice-agent/config";
}

function fromBody(b: VoiceAgentConfigBody): VoiceAgentConfig {
  return {
    enabled: b.enabled,
    greeting: b.greeting,
    transferNumber: b.transfer_number,
    timezone: b.timezone,
    openTime: b.open_time,
    closeTime: b.close_time,
    openDays: b.open_days,
    maxMinutes: b.max_minutes,
    plivoReady: b.plivo_ready,
  };
}

function toError(error: unknown): { status: "error"; message: string; correlationId: string } {
  if (error instanceof AdminApiError) {
    let message = `Something went wrong (${error.errorCode || "UNKNOWN_ERROR"}). Correlation ID: ${
      error.correlationId || "n/a"
    }.`;
    if (error.status === 403 || error.errorCode === "ROLE_NOT_PERMITTED") {
      message = "You do not have permission to change this.";
    } else if (error.status === 401) {
      message = "Your session has expired. Please log in again.";
    } else if (error.status === 422) {
      message =
        "Please check the fields: a full phone number, a valid time zone, times as HH:MM, and a call limit of 1-60 minutes.";
    }
    return { status: "error", message, correlationId: error.correlationId };
  }
  return { status: "error", message: "Unable to reach the server. Please try again.", correlationId: "" };
}

export async function getVoiceAgentConfig(tenantId?: string): Promise<VoiceAgentConfigResult> {
  try {
    const response = await adminApiFetch(path(tenantId));
    return { status: "ok", config: fromBody((await response.json()) as VoiceAgentConfigBody) };
  } catch (error) {
    return toError(error);
  }
}

export async function saveVoiceAgentConfig(
  config: Omit<VoiceAgentConfig, "plivoReady">,
  tenantId?: string
): Promise<VoiceAgentConfigResult> {
  try {
    const response = await adminApiFetch(path(tenantId), {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        enabled: config.enabled,
        greeting: config.greeting,
        transfer_number: config.transferNumber,
        timezone: config.timezone,
        open_time: config.openTime,
        close_time: config.closeTime,
        open_days: config.openDays,
        max_minutes: config.maxMinutes,
      }),
    });
    return { status: "ok", config: fromBody((await response.json()) as VoiceAgentConfigBody) };
  } catch (error) {
    return toError(error);
  }
}

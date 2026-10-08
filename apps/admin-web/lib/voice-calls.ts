/**
 * Server functions for the AI voice confirmation call -- `"use server"`,
 * same one-file shape as `lib/calls.ts` (called from the self-contained
 * settings card and from server components alike).
 *
 * - `GET/PUT /admin/calls/voice-config` (CLIENT_ADMIN)
 * - `GET /admin/calls/leads/{leadId}/voice-calls` (CLIENT_ADMIN/CLIENT_AGENT)
 * Each has the `/admin/tenants/{tenantId}/calls/...` PLATFORM_ADMIN twin.
 */
"use server";

import { adminApiFetch, AdminApiError } from "@/lib/api";

export interface VoiceCallConfig {
  enabled: boolean;
  questions: string[];
}

export interface VoiceCallTranscriptEntry {
  question: string;
  heard: string;
  /** "yes" | "no" | "unclear" | "no_response" */
  answer: string;
}

export interface VoiceCall {
  callId: string;
  /** "queued" | "calling" | "in_progress" | "completed" | "no_answer" | "busy" | "failed" */
  status: string;
  toNumber: string;
  createdAt: string;
  lastError: string | null;
  transcript: VoiceCallTranscriptEntry[];
}

interface VoiceCallResponseBody {
  call_id: string;
  status: string;
  to_number: string;
  created_at: string;
  last_error: string | null;
  transcript: VoiceCallTranscriptEntry[];
}

export type VoiceCallConfigResult =
  | { status: "ok"; config: VoiceCallConfig }
  | { status: "error"; message: string; correlationId: string };

export type VoiceCallsResult =
  | { status: "ok"; calls: VoiceCall[] }
  | { status: "error"; message: string; correlationId: string };

function basePath(tenantId?: string): string {
  return tenantId ? `/admin/tenants/${encodeURIComponent(tenantId)}/calls` : "/admin/calls";
}

function toError(error: unknown): { status: "error"; message: string; correlationId: string } {
  if (error instanceof AdminApiError) {
    let message = `Something went wrong (${error.errorCode || "UNKNOWN_ERROR"}). Correlation ID: ${
      error.correlationId || "n/a"
    }.`;
    if (error.status === 403 || error.errorCode === "ROLE_NOT_PERMITTED") {
      message = "You do not have permission to view this.";
    } else if (error.status === 401) {
      message = "Your session has expired. Please log in again.";
    } else if (error.status === 422) {
      message = "Please add at least one question (up to 5, each under 300 characters).";
    }
    return { status: "error", message, correlationId: error.correlationId };
  }
  return { status: "error", message: "Unable to reach the server. Please try again.", correlationId: "" };
}

export async function getVoiceCallConfig(tenantId?: string): Promise<VoiceCallConfigResult> {
  try {
    const response = await adminApiFetch(`${basePath(tenantId)}/voice-config`);
    const config = (await response.json()) as VoiceCallConfig;
    return { status: "ok", config };
  } catch (error) {
    return toError(error);
  }
}

export async function saveVoiceCallConfig(
  enabled: boolean,
  questions: string[],
  tenantId?: string
): Promise<VoiceCallConfigResult> {
  try {
    const response = await adminApiFetch(`${basePath(tenantId)}/voice-config`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled, questions }),
    });
    const config = (await response.json()) as VoiceCallConfig;
    return { status: "ok", config };
  } catch (error) {
    return toError(error);
  }
}

export async function getLeadVoiceCalls(leadId: string, tenantId?: string): Promise<VoiceCallsResult> {
  try {
    const response = await adminApiFetch(
      `${basePath(tenantId)}/leads/${encodeURIComponent(leadId)}/voice-calls`
    );
    const body = (await response.json()) as VoiceCallResponseBody[];
    return {
      status: "ok",
      calls: body.map((c) => ({
        callId: c.call_id,
        status: c.status,
        toNumber: c.to_number,
        createdAt: c.created_at,
        lastError: c.last_error,
        transcript: c.transcript,
      })),
    };
  } catch (error) {
    return toError(error);
  }
}

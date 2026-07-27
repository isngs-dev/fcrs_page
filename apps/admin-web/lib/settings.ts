/**
 * Server-only data layer for the tenant bot-settings screen (S13.6).
 * Mirrors `lib/leads.ts`'s shape (decision 8): a typed `getBotSettings()`
 * calling `GET /admin/settings` via `adminApiFetch`, mapping the response
 * (or any error) into a discriminated `SettingsResult` the page renders
 * directly -- no silent fallbacks (CLAUDE.md §3): a backend error always
 * becomes a visible, honest state, never a blank/faked form.
 */
import "server-only";

import { adminApiFetch, AdminApiError } from "@/lib/api";

/** Camel-cased mirror of `AdminBotSettingsResponse`
 * (services/api/src/api/admin/settings_routes.py:36-47). `null`s are
 * preserved verbatim -- never coerced to `""`/`0` (no-silent-fallback). */
export interface BotSettings {
  greeting: string | null;
  launcherLabel: string | null;
  sidebarWorkspaceLabel: string | null;
  dashboardTitle: string | null;
  businessHours: Record<string, unknown> | null;
  escalationPolicy: string | null;
  tone: string | null;
  answerThreshold: number;
  escalateThreshold: number;
  turnCap: number;
  llmProvider: string | null;
  llmModel: string | null;
}

interface AdminBotSettingsResponseBody {
  greeting: string | null;
  launcher_label: string | null;
  sidebar_workspace_label: string | null;
  dashboard_title: string | null;
  business_hours: Record<string, unknown> | null;
  escalation_policy: string | null;
  tone: string | null;
  answer_threshold: number;
  escalate_threshold: number;
  turn_cap: number;
  llm_provider: string | null;
  llm_model: string | null;
}

export type SettingsResult =
  | { status: "ok"; settings: BotSettings }
  | { status: "error"; message: string; correlationId: string };

function toBotSettings(body: AdminBotSettingsResponseBody): BotSettings {
  return {
    greeting: body.greeting,
    launcherLabel: body.launcher_label,
    sidebarWorkspaceLabel: body.sidebar_workspace_label ?? null,
    dashboardTitle: body.dashboard_title ?? null,
    businessHours: body.business_hours,
    escalationPolicy: body.escalation_policy,
    tone: body.tone,
    answerThreshold: body.answer_threshold,
    escalateThreshold: body.escalate_threshold,
    turnCap: body.turn_cap,
    llmProvider: body.llm_provider,
    llmModel: body.llm_model,
  };
}

/**
 * Fetch the caller's tenant bot settings. Never sends a `tenant_id` in the
 * BODY -- scoping is entirely the backend's repository-layer job from the
 * caller's own claims (CLAUDE.md §3). Never logs the response body.
 *
 * `tenantId` (S13.7): when provided, targets the S12.7 PLATFORM_ADMIN
 * super-user surface `GET /admin/tenants/{tenantId}/settings` instead of the
 * implicit `GET /admin/settings` -- the SAME response shape
 * (settings_routes.py `_get_settings`), only the URL prefix differs. The
 * `{tenantId}` always comes from the route segment the per-client screen is
 * mounted under, never client state (D1).
 */
export async function getBotSettings(tenantId?: string): Promise<SettingsResult> {
  try {
    const path = tenantId
      ? `/admin/tenants/${encodeURIComponent(tenantId)}/settings`
      : "/admin/settings";
    const response = await adminApiFetch(path);
    const body = (await response.json()) as AdminBotSettingsResponseBody;
    return { status: "ok", settings: toBotSettings(body) };
  } catch (error) {
    if (error instanceof AdminApiError) {
      return {
        status: "error",
        message: mapErrorMessage(error),
        correlationId: error.correlationId,
      };
    }
    // A network throw (not an AdminApiError) -- the request never reached
    // (or never returned from) admin-api.
    return {
      status: "error",
      message: "Unable to reach the server. Please try again.",
      correlationId: "",
    };
  }
}

function mapErrorMessage(error: AdminApiError): string {
  if (error.status === 403 || error.errorCode === "ROLE_NOT_PERMITTED") {
    return "You do not have permission to view these settings.";
  }
  if (error.status === 401) {
    return "Your session has expired. Please log in again.";
  }
  return `Something went wrong (${error.errorCode || "UNKNOWN_ERROR"}). Correlation ID: ${
    error.correlationId || "n/a"
  }.`;
}

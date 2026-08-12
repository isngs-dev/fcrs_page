"use server";

/**
 * Tenant bot-settings save action (S13.6 decisions 4, 5, 7). Validates with
 * the shared Zod schema + `parseBusinessHours` (courtesy pre-check; the
 * backend is authoritative), then calls `PUT /admin/settings` via
 * `adminApiFetch`.
 *
 * CONFIRMED, not optimistic (decision 4): on a `200`, the returned state
 * carries the values from the PUT *response body* (the server's
 * freshly-persisted unified settings, `settings_routes.py:99-100`), never
 * the raw submitted `formData` -- so a backend-side normalization or
 * concurrent change is reflected honestly. `revalidatePath("/settings")`
 * re-syncs the RSC so a subsequent reload/navigation is consistent.
 *
 * Never logs the response body (mirrors `tenants/new/actions.ts`'s secrets
 * hygiene, applied here to tenant config rather than credentials).
 */
import { revalidatePath } from "next/cache";
import { AdminApiError, adminApiFetch } from "@/lib/api";
import {
  parseBusinessHours,
  settingsFormSchema,
} from "@/lib/settings-schema";
import type { BotSettings } from "@/lib/settings";

export interface SaveFieldErrors {
  greeting?: string;
  launcherLabel?: string;
  sidebarWorkspaceLabel?: string;
  dashboardTitle?: string;
  escalationPolicy?: string;
  tone?: string;
  businessHoursText?: string;
  turnCap?: string;
}

export interface SaveIdleState {
  status: "idle";
}

export interface SaveErrorState {
  status: "error";
  fieldErrors: SaveFieldErrors;
  /** General/banner-level message, when not tied to a single field. */
  formError: string | null;
  correlationId: string | null;
}

export interface SaveSuccessState {
  status: "saved";
  settings: BotSettings;
}

export type SaveState = SaveIdleState | SaveErrorState | SaveSuccessState;

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

const GENERIC_NETWORK_ERROR = "Unable to reach the server. Please try again.";

function errorState(partial: Omit<SaveErrorState, "status">): SaveErrorState {
  return { status: "error", ...partial };
}

/**
 * `tenantId` (S13.7): bound via `saveSettings.bind(null, tenantId)` from the
 * per-client settings screen -- when set, targets the S12.7 PLATFORM_ADMIN
 * super-user surface `PUT /admin/tenants/{tenantId}/settings` instead of the
 * implicit `PUT /admin/settings`, and revalidates `/clients/{tenantId}/settings`
 * instead of `/settings`. `undefined`/omitted preserves the existing
 * CLIENT_ADMIN behavior exactly.
 */
export async function saveSettings(
  tenantId: string | undefined,
  _prevState: SaveState,
  formData: FormData
): Promise<SaveState> {
  const nullToUndefined = (value: FormDataEntryValue | null): string | undefined =>
    value === null ? undefined : String(value);

  const parsed = settingsFormSchema.safeParse({
    greeting: nullToUndefined(formData.get("greeting")),
    launcherLabel: nullToUndefined(formData.get("launcherLabel")),
    sidebarWorkspaceLabel: nullToUndefined(formData.get("sidebarWorkspaceLabel")),
    dashboardTitle: nullToUndefined(formData.get("dashboardTitle")),
    escalationPolicy: nullToUndefined(formData.get("escalationPolicy")),
    tone: nullToUndefined(formData.get("tone")),
    businessHoursText: String(formData.get("businessHoursText") ?? ""),
    turnCap: nullToUndefined(formData.get("turnCap")),
  });

  if (!parsed.success) {
    const fieldErrors: SaveFieldErrors = {};
    for (const issue of parsed.error.issues) {
      const key = issue.path[0];
      if (key === "greeting") fieldErrors.greeting ??= issue.message;
      else if (key === "launcherLabel") fieldErrors.launcherLabel ??= issue.message;
      else if (key === "sidebarWorkspaceLabel") fieldErrors.sidebarWorkspaceLabel ??= issue.message;
      else if (key === "dashboardTitle") fieldErrors.dashboardTitle ??= issue.message;
      else if (key === "escalationPolicy") fieldErrors.escalationPolicy ??= issue.message;
      else if (key === "tone") fieldErrors.tone ??= issue.message;
      else if (key === "turnCap") fieldErrors.turnCap ??= issue.message;
    }
    return errorState({
      fieldErrors,
      formError: Object.keys(fieldErrors).length === 0 ? "Check the form and try again." : null,
      correlationId: null,
    });
  }

  const {
    greeting,
    launcherLabel,
    sidebarWorkspaceLabel,
    dashboardTitle,
    escalationPolicy,
    tone,
    businessHoursText,
    turnCap,
  } = parsed.data;

  // Belt-and-suspenders JSON guard (decision 5) -- reject client-side and
  // never call the backend on invalid business_hours.
  const businessHoursResult = parseBusinessHours(businessHoursText);
  if (!businessHoursResult.ok) {
    return errorState({
      fieldErrors: { businessHoursText: businessHoursResult.error },
      formError: null,
      correlationId: null,
    });
  }

  const requestBody = {
    greeting: greeting ?? null,
    launcher_label: launcherLabel ?? null,
    sidebar_workspace_label: sidebarWorkspaceLabel ?? null,
    dashboard_title: dashboardTitle ?? null,
    escalation_policy: escalationPolicy ?? null,
    tone: tone ?? null,
    business_hours: businessHoursResult.value,
    // `null` when the field was left blank -- `settings_routes.py` treats a
    // `None` turn_cap as "not provided" and leaves
    // `tenant_orchestrator_configs` untouched (never resets it to a
    // default), mirroring how a blank field here means "no change".
    turn_cap: turnCap ?? null,
  };

  const path = tenantId
    ? `/admin/tenants/${encodeURIComponent(tenantId)}/settings`
    : "/admin/settings";

  let response: Response;
  try {
    response = await adminApiFetch(path, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(requestBody),
    });
  } catch (err) {
    if (err instanceof AdminApiError) {
      return mapAdminApiError(err);
    }
    // Network-level throw (not an AdminApiError).
    return errorState({
      fieldErrors: {},
      formError: GENERIC_NETWORK_ERROR,
      correlationId: null,
    });
  }

  const body = (await response.json()) as AdminBotSettingsResponseBody;

  revalidatePath(tenantId ? `/clients/${tenantId}/settings` : "/settings");
  if (!tenantId) revalidatePath("/");

  return { status: "saved", settings: toBotSettings(body) };
}

function mapAdminApiError(err: AdminApiError): SaveErrorState {
  if (err.status === 403 || err.errorCode === "ROLE_NOT_PERMITTED") {
    return errorState({
      fieldErrors: {},
      formError: "You do not have permission to change these settings.",
      correlationId: err.correlationId || null,
    });
  }

  if (err.status === 401) {
    return errorState({
      fieldErrors: {},
      formError: "Your session has expired. Please sign in again.",
      correlationId: err.correlationId || null,
    });
  }

  if (err.status === 422) {
    // Pydantic body-validation 422s arrive in FastAPI's default `{detail}`
    // shape (no `RequestValidationError` handler is registered -- see
    // S13.6.md Investigation), so `err.errorCode`/`err.message` may be
    // empty/undefined here. The client Zod pre-check mirrors the backend's
    // max_lengths, so this path is belt-and-suspenders, not the normal one.
    return errorState({
      fieldErrors: {},
      formError: "The server rejected one or more values — check the lengths and try again.",
      correlationId: err.correlationId || null,
    });
  }

  return errorState({
    fieldErrors: {},
    formError: `${err.message || "Something went wrong."} (correlation ID: ${
      err.correlationId || "unknown"
    })`,
    correlationId: err.correlationId || null,
  });
}

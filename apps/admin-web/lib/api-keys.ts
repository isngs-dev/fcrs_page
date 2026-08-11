/**
 * Server-only data layer for the API-keys settings screen (SR-20 D6).
 * `getApiKeyInfo()` calls `GET /admin/api-keys` -- NEVER returns raw or
 * hashed key material (the backend structurally cannot leak it: the
 * repository never selects the hash column into this response shape). The
 * only place a raw key ever appears is the one-time rotation response,
 * handled by the `rotateApiKey` server action, never persisted client-side
 * beyond the reveal.
 */
import "server-only";

import { adminApiFetch, AdminApiError } from "@/lib/api";

export interface ApiKeyInfo {
  hasKey: boolean;
  allowedOrigins: string[];
}

interface ApiKeyInfoResponseBody {
  has_key: boolean;
  allowed_origins: string[];
}

export type ApiKeyInfoResult =
  | { status: "ok"; info: ApiKeyInfo }
  | { status: "error"; message: string; correlationId: string };

export async function getApiKeyInfo(): Promise<ApiKeyInfoResult> {
  try {
    const response = await adminApiFetch("/admin/api-keys");
    const body = (await response.json()) as ApiKeyInfoResponseBody;
    return {
      status: "ok",
      info: { hasKey: body.has_key, allowedOrigins: body.allowed_origins },
    };
  } catch (error) {
    if (error instanceof AdminApiError) {
      return {
        status: "error",
        message: mapErrorMessage(error),
        correlationId: error.correlationId,
      };
    }
    return {
      status: "error",
      message: "Unable to reach the server. Please try again.",
      correlationId: "",
    };
  }
}

function mapErrorMessage(error: AdminApiError): string {
  if (error.status === 403 || error.errorCode === "ROLE_NOT_PERMITTED") {
    return "You do not have permission to view API keys.";
  }
  if (error.status === 401) {
    return "Your session has expired. Please log in again.";
  }
  return `Something went wrong (${error.errorCode || "UNKNOWN_ERROR"}). Correlation ID: ${
    error.correlationId || "n/a"
  }.`;
}

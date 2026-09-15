/**
 * Server-only data layer for the "AI provider" settings screen. `getLlmConfig()`
 * calls `GET /admin/llm/config` -- NEVER returns raw/decrypted key material
 * (the backend's `get_llm_config_summary` structurally cannot select the
 * ciphertext columns into this response shape, mirroring `lib/api-keys.ts`'s
 * `has_key`-boolean pattern). An unconfigured tenant is an honest all-null/
 * false response, never a 404.
 */
import "server-only";

import { adminApiFetch, AdminApiError } from "@/lib/api";

export interface LlmConfig {
  provider: string | null;
  model: string | null;
  baseUrl: string | null;
  apiVersion: string | null;
  embeddingModel: string | null;
  embeddingBaseUrl: string | null;
  embeddingDimensions: number | null;
  hasApiKey: boolean;
  hasEmbeddingApiKey: boolean;
}

interface LlmConfigResponseBody {
  provider: string | null;
  model: string | null;
  base_url: string | null;
  api_version: string | null;
  embedding_model: string | null;
  embedding_base_url: string | null;
  embedding_dimensions: number | null;
  has_api_key: boolean;
  has_embedding_api_key: boolean;
}

export type LlmConfigResult =
  | { status: "ok"; config: LlmConfig }
  | { status: "error"; message: string; correlationId: string };

function toLlmConfig(body: LlmConfigResponseBody): LlmConfig {
  return {
    provider: body.provider,
    model: body.model,
    baseUrl: body.base_url,
    apiVersion: body.api_version,
    embeddingModel: body.embedding_model,
    embeddingBaseUrl: body.embedding_base_url,
    embeddingDimensions: body.embedding_dimensions,
    hasApiKey: body.has_api_key,
    hasEmbeddingApiKey: body.has_embedding_api_key,
  };
}

export async function getLlmConfig(): Promise<LlmConfigResult> {
  try {
    const response = await adminApiFetch("/admin/llm/config");
    const body = (await response.json()) as LlmConfigResponseBody;
    return { status: "ok", config: toLlmConfig(body) };
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
    return "You do not have permission to view AI provider settings.";
  }
  if (error.status === 401) {
    return "Your session has expired. Please log in again.";
  }
  return `Something went wrong (${error.errorCode || "UNKNOWN_ERROR"}). Correlation ID: ${
    error.correlationId || "n/a"
  }.`;
}

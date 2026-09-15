"use server";

/**
 * "AI provider" settings server action -- saves the tenant's LLM/embedding
 * provider config via `POST /admin/llm/config` (the new real route,
 * services/api/src/api/llm/admin_routes.py). Mirrors `calendly-actions.ts`'s
 * Zod-validated shape, with one difference: the credential fields are
 * OPTIONAL -- a blank field means "keep the existing key", not "clear it"
 * (the backend preserves it via `COALESCE`). Required only the very first
 * time this tenant configures a provider (422 `API_KEY_REQUIRED` otherwise).
 */
import { revalidatePath } from "next/cache";
import { z } from "zod";
import { AdminApiError, adminApiFetch } from "@/lib/api";
import type { LlmConfig } from "@/lib/llm-config";

const PROVIDERS = ["anthropic", "openai", "azure"] as const;

// `.nullish()`, not `.optional()`: a FormData field this app never sets
// (e.g. `formData.get("apiKey")` when the input was left blank) comes back
// as `null`, not `undefined` -- `.optional()` alone rejects `null`.
const llmConfigFormSchema = z.object({
  provider: z.enum(PROVIDERS, { message: "Choose a provider." }),
  model: z.string().trim().min(1, "Model is required.").max(200),
  apiKey: z
    .string()
    .max(4000)
    .nullish()
    .transform((value) => (value && value.trim().length > 0 ? value.trim() : undefined)),
  baseUrl: z
    .string()
    .max(500)
    .nullish()
    .transform((value) => (value && value.trim().length > 0 ? value.trim() : undefined)),
  apiVersion: z
    .string()
    .max(50)
    .nullish()
    .transform((value) => (value && value.trim().length > 0 ? value.trim() : undefined)),
  embeddingModel: z
    .string()
    .max(200)
    .nullish()
    .transform((value) => (value && value.trim().length > 0 ? value.trim() : undefined)),
  embeddingBaseUrl: z
    .string()
    .max(500)
    .nullish()
    .transform((value) => (value && value.trim().length > 0 ? value.trim() : undefined)),
  embeddingApiKey: z
    .string()
    .max(4000)
    .nullish()
    .transform((value) => (value && value.trim().length > 0 ? value.trim() : undefined)),
  embeddingDimensions: z
    .string()
    .nullish()
    .transform((value) => (value && value.trim().length > 0 ? Number(value.trim()) : undefined))
    .refine((value) => value === undefined || (Number.isInteger(value) && value > 0 && value <= 8192), {
      message: "Enter a whole number between 1 and 8192.",
    }),
});

export interface SaveLlmConfigFieldErrors {
  provider?: string;
  model?: string;
  credential?: string;
  embeddingDimensions?: string;
}

export interface SaveLlmConfigIdleState {
  status: "idle";
}

export interface SaveLlmConfigErrorState {
  status: "error";
  fieldErrors: SaveLlmConfigFieldErrors;
  formError: string | null;
}

export interface SaveLlmConfigSuccessState {
  status: "saved";
  config: LlmConfig;
}

export type SaveLlmConfigState =
  | SaveLlmConfigIdleState
  | SaveLlmConfigErrorState
  | SaveLlmConfigSuccessState;

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

function errorState(partial: Omit<SaveLlmConfigErrorState, "status">): SaveLlmConfigErrorState {
  return { status: "error", ...partial };
}

export async function saveLlmConfig(
  _prevState: SaveLlmConfigState,
  formData: FormData
): Promise<SaveLlmConfigState> {
  const parsed = llmConfigFormSchema.safeParse({
    provider: formData.get("provider"),
    model: formData.get("model"),
    apiKey: formData.get("apiKey"),
    baseUrl: formData.get("baseUrl"),
    apiVersion: formData.get("apiVersion"),
    embeddingModel: formData.get("embeddingModel"),
    embeddingBaseUrl: formData.get("embeddingBaseUrl"),
    embeddingApiKey: formData.get("embeddingApiKey"),
    embeddingDimensions: formData.get("embeddingDimensions"),
  });

  if (!parsed.success) {
    const fieldErrors: SaveLlmConfigFieldErrors = {};
    for (const issue of parsed.error.issues) {
      const key = issue.path[0];
      if (key === "provider") fieldErrors.provider ??= issue.message;
      else if (key === "model") fieldErrors.model ??= issue.message;
      else if (key === "embeddingDimensions") fieldErrors.embeddingDimensions ??= issue.message;
    }
    return errorState({
      fieldErrors,
      formError: Object.keys(fieldErrors).length === 0 ? "Check the form and try again." : null,
    });
  }

  const data = parsed.data;
  const credentialFields: Record<string, unknown> = {};
  if (data.apiKey !== undefined) credentialFields.api_key = data.apiKey;
  if (data.embeddingApiKey !== undefined) credentialFields.embedding_api_key = data.embeddingApiKey;

  const requestBody: Record<string, unknown> = {
    provider: data.provider,
    model: data.model,
    ...(data.baseUrl !== undefined ? { base_url: data.baseUrl } : {}),
    ...(data.apiVersion !== undefined ? { api_version: data.apiVersion } : {}),
    ...(data.embeddingModel !== undefined ? { embedding_model: data.embeddingModel } : {}),
    ...(data.embeddingBaseUrl !== undefined ? { embedding_base_url: data.embeddingBaseUrl } : {}),
    ...(data.embeddingDimensions !== undefined
      ? { embedding_dimensions: data.embeddingDimensions }
      : {}),
    ...credentialFields,
  };

  let response: Response;
  try {
    response = await adminApiFetch("/admin/llm/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(requestBody),
    });
  } catch (err) {
    if (err instanceof AdminApiError) {
      return mapSaveError(err);
    }
    return errorState({
      fieldErrors: {},
      formError: "Unable to reach the server. Please try again.",
    });
  }

  const body = (await response.json()) as LlmConfigResponseBody;
  revalidatePath("/workspace");

  return {
    status: "saved",
    config: {
      provider: body.provider,
      model: body.model,
      baseUrl: body.base_url,
      apiVersion: body.api_version,
      embeddingModel: body.embedding_model,
      embeddingBaseUrl: body.embedding_base_url,
      embeddingDimensions: body.embedding_dimensions,
      hasApiKey: body.has_api_key,
      hasEmbeddingApiKey: body.has_embedding_api_key,
    },
  };
}

const FIRST_TIME_CREDENTIAL_MESSAGE =
  "Enter an API key — this is the first time you're configuring a provider for this chatbot.";
const PERMISSION_MESSAGE = "You do not have permission to change AI provider settings.";
const SESSION_EXPIRED_MESSAGE = "Your session has expired. Please sign in again.";

function mapSaveError(err: AdminApiError): SaveLlmConfigErrorState {
  if (err.errorCode === "API_KEY_REQUIRED") {
    return errorState({
      fieldErrors: { credential: FIRST_TIME_CREDENTIAL_MESSAGE },
      formError: null,
    });
  }
  if (err.status === 403 || err.errorCode === "ROLE_NOT_PERMITTED") {
    return errorState({ fieldErrors: {}, formError: PERMISSION_MESSAGE });
  }
  if (err.status === 401) {
    return errorState({ fieldErrors: {}, formError: SESSION_EXPIRED_MESSAGE });
  }
  return errorState({
    fieldErrors: {},
    formError: `${err.message || "Something went wrong."} (correlation ID: ${
      err.correlationId || "unknown"
    })`,
  });
}

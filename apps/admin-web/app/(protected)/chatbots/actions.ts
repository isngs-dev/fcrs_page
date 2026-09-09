"use server";

/**
 * CLIENT_ADMIN self-service "create a chatbot" server action (multi-chatbot
 * accounts). Wraps `POST /admin/tenants/mine` via `lib/chatbots.ts`'s thin
 * helper -- mirrors `clients/actions.ts#onboardNewClient`'s shape, minus the
 * admin-user/password fields (this endpoint creates no user; every existing
 * member of the account already reaches the new chatbot via the switcher).
 *
 * Secrets hygiene (matching `clients/actions.ts`'s established pattern): the
 * one-time `client_key` is returned to the caller as `useActionState` result
 * state and is NEVER logged here.
 */
import { revalidatePath } from "next/cache";
import { z } from "zod";
import { AdminApiError } from "@/lib/api";
import { createMyChatbot } from "@/lib/chatbots";

const SLUG_PATTERN = /^[a-z0-9]([a-z0-9-]*[a-z0-9])?$/;

const createChatbotSchema = z.object({
  name: z
    .string()
    .trim()
    .min(1, "Name is required.")
    .max(200, "Name must be 200 characters or fewer."),
  slug: z
    .string()
    .trim()
    .min(1, "Slug is required.")
    .max(63, "Slug must be 63 characters or fewer.")
    .regex(
      SLUG_PATTERN,
      "Lowercase letters, numbers, and single hyphens; must start and end alphanumeric."
    ),
});

export interface CreateChatbotFieldErrors {
  name?: string;
  slug?: string;
}

export interface CreateChatbotCreatedResult {
  status: "created";
  tenantId: string;
  name: string;
  slug: string;
  clientKey: string;
}

export interface CreateChatbotErrorResult {
  status: "error";
  fieldErrors: CreateChatbotFieldErrors;
  formError: string | null;
  correlationId: string | null;
}

export interface CreateChatbotIdleResult {
  status: "idle";
}

export type CreateChatbotState =
  | CreateChatbotIdleResult
  | CreateChatbotErrorResult
  | CreateChatbotCreatedResult;

const GENERIC_NETWORK_ERROR = "Unable to reach the server. Please try again.";

function errorState(partial: Omit<CreateChatbotErrorResult, "status">): CreateChatbotErrorResult {
  return { status: "error", ...partial };
}

export async function createChatbotAction(
  _prevState: CreateChatbotState,
  formData: FormData
): Promise<CreateChatbotState> {
  const parsed = createChatbotSchema.safeParse({
    name: formData.get("name"),
    slug: formData.get("slug"),
  });

  if (!parsed.success) {
    const fieldErrors: CreateChatbotFieldErrors = {};
    for (const issue of parsed.error.issues) {
      const key = issue.path[0];
      if (key === "name") fieldErrors.name ??= issue.message;
      else if (key === "slug") fieldErrors.slug ??= issue.message;
    }
    return errorState({
      fieldErrors,
      formError: Object.keys(fieldErrors).length === 0 ? "Check the form and try again." : null,
      correlationId: null,
    });
  }

  const { name, slug } = parsed.data;

  let body: Awaited<ReturnType<typeof createMyChatbot>>;
  try {
    body = await createMyChatbot({ name, slug });
  } catch (err) {
    if (err instanceof AdminApiError) {
      return mapCreateError(err);
    }
    return errorState({ fieldErrors: {}, formError: GENERIC_NETWORK_ERROR, correlationId: null });
  }

  revalidatePath("/chatbots");

  return {
    status: "created",
    tenantId: body.tenant_id,
    name: body.name,
    slug: body.slug,
    clientKey: body.client_key,
  };
}

function mapCreateError(err: AdminApiError): CreateChatbotErrorResult {
  if (err.errorCode === "TENANT_SLUG_TAKEN") {
    return errorState({
      fieldErrors: { slug: "That slug is already taken — choose another." },
      formError: null,
      correlationId: err.correlationId || null,
    });
  }
  if (err.status === 403 || err.errorCode === "ROLE_NOT_PERMITTED") {
    return errorState({
      fieldErrors: {},
      formError: "You do not have permission to create a chatbot.",
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
  return errorState({
    fieldErrors: {},
    formError: `${err.message} (correlation ID: ${err.correlationId || "unknown"})`,
    correlationId: err.correlationId || null,
  });
}

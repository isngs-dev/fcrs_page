"use server";

/**
 * API-keys server actions (SR-20 D6): rotate the tenant's own client key
 * (show-once), and update the Origin allowlist.
 *
 * Rotation hygiene: the raw key returned here is passed straight into the
 * component's state for a one-time reveal and is never logged, never
 * written to any persistent client-side store, and this action itself
 * never logs the response body (mirrors `settings/actions.ts`'s secrets
 * discipline, applied to a real secret this time rather than tenant
 * config).
 */
import { revalidatePath } from "next/cache";
import { AdminApiError, adminApiFetch } from "@/lib/api";

export interface RotateKeyIdleState {
  status: "idle";
}

export interface RotateKeyErrorState {
  status: "error";
  message: string;
  correlationId: string | null;
}

export interface RotateKeySuccessState {
  status: "rotated";
  clientKey: string;
}

export type RotateKeyState = RotateKeyIdleState | RotateKeyErrorState | RotateKeySuccessState;

export async function rotateApiKey(): Promise<RotateKeyState> {
  try {
    const response = await adminApiFetch("/admin/api-keys/rotate", { method: "POST" });
    const body = (await response.json()) as { client_key: string };
    revalidatePath("/settings");
    return { status: "rotated", clientKey: body.client_key };
  } catch (err) {
    if (err instanceof AdminApiError) {
      return mapRotateError(err);
    }
    return {
      status: "error",
      message: "Unable to reach the server. Please try again.",
      correlationId: null,
    };
  }
}

function mapRotateError(err: AdminApiError): RotateKeyErrorState {
  if (err.status === 403 || err.errorCode === "ROLE_NOT_PERMITTED") {
    return {
      status: "error",
      message: "You do not have permission to rotate the client key.",
      correlationId: err.correlationId || null,
    };
  }
  if (err.status === 401) {
    return {
      status: "error",
      message: "Your session has expired. Please sign in again.",
      correlationId: err.correlationId || null,
    };
  }
  return {
    status: "error",
    message: `${err.message || "Something went wrong."} (correlation ID: ${
      err.correlationId || "unknown"
    })`,
    correlationId: err.correlationId || null,
  };
}

export interface UpdateOriginsIdleState {
  status: "idle";
}

export interface UpdateOriginsErrorState {
  status: "error";
  message: string;
  correlationId: string | null;
}

export interface UpdateOriginsSuccessState {
  status: "saved";
  allowedOrigins: string[];
}

export type UpdateOriginsState =
  | UpdateOriginsIdleState
  | UpdateOriginsErrorState
  | UpdateOriginsSuccessState;

export async function updateOrigins(
  _prevState: UpdateOriginsState,
  formData: FormData
): Promise<UpdateOriginsState> {
  const raw = String(formData.get("origins") ?? "");
  const origins = raw
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line.length > 0);

  let response: Response;
  try {
    response = await adminApiFetch("/admin/api-keys/origins", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ origins }),
    });
  } catch (err) {
    if (err instanceof AdminApiError) {
      return mapOriginsError(err);
    }
    return {
      status: "error",
      message: "Unable to reach the server. Please try again.",
      correlationId: null,
    };
  }

  const body = (await response.json()) as { allowed_origins: string[] };
  revalidatePath("/settings");
  return { status: "saved", allowedOrigins: body.allowed_origins };
}

function mapOriginsError(err: AdminApiError): UpdateOriginsErrorState {
  if (err.status === 403 || err.errorCode === "ROLE_NOT_PERMITTED") {
    return {
      status: "error",
      message: "You do not have permission to change the Origin allowlist.",
      correlationId: err.correlationId || null,
    };
  }
  if (err.status === 401) {
    return {
      status: "error",
      message: "Your session has expired. Please sign in again.",
      correlationId: err.correlationId || null,
    };
  }
  if (err.errorCode === "INVALID_ORIGIN") {
    return {
      status: "error",
      message:
        err.message ||
        "One or more origins are invalid. Use the form https://example.com — no path, no wildcard.",
      correlationId: err.correlationId || null,
    };
  }
  return {
    status: "error",
    message: `${err.message || "Something went wrong."} (correlation ID: ${
      err.correlationId || "unknown"
    })`,
    correlationId: err.correlationId || null,
  };
}

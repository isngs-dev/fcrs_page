"use server";

/**
 * Team members server actions (7b). Two actions, mirroring the
 * `useActionState` pattern from `tenants/new/actions.ts` (create + one-time
 * secret reveal) and `settings/actions.ts` (confirmed, not optimistic --
 * the returned state carries the server's response, never the raw submitted
 * form data).
 *
 * Secrets hygiene (highest priority, matches `tenants/new/actions.ts`):
 * `createMember`'s one-time `temp_password` is returned to the client as
 * `useActionState` state and is NEVER logged here -- only
 * `error_code`/`correlation_id`/`message` from `AdminApiError` are
 * logged/rendered on error paths.
 */
import { revalidatePath } from "next/cache";
import { AdminApiError } from "@/lib/api";
import { createMember, deleteMember, setMemberActive, type MemberSummary } from "@/lib/members";

// ---------------------------------------------------------------------------
// Create member
// ---------------------------------------------------------------------------

export interface CreateMemberFieldErrors {
  email?: string;
  name?: string;
}

export interface CreateMemberIdleState {
  status: "idle";
}

export interface CreateMemberErrorState {
  status: "error";
  fieldErrors: CreateMemberFieldErrors;
  formError: string | null;
  correlationId: string | null;
}

export interface CreateMemberSuccessState {
  status: "created";
  member: {
    id: string;
    email: string;
    name: string | null;
    role: string;
  };
  tempPassword: string;
}

export type CreateMemberState =
  | CreateMemberIdleState
  | CreateMemberErrorState
  | CreateMemberSuccessState;

const GENERIC_NETWORK_ERROR = "Unable to reach the server. Please try again.";
const EMAIL_PATTERN = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;

function createErrorState(partial: Omit<CreateMemberErrorState, "status">): CreateMemberErrorState {
  return { status: "error", ...partial };
}

export async function createMemberAction(
  _prevState: CreateMemberState,
  formData: FormData
): Promise<CreateMemberState> {
  const email = String(formData.get("email") ?? "").trim();
  const nameRaw = formData.get("name");
  const name = nameRaw === null ? undefined : String(nameRaw).trim() || undefined;

  const fieldErrors: CreateMemberFieldErrors = {};
  if (!email) {
    fieldErrors.email = "Email is required.";
  } else if (email.length < 3 || email.length > 254 || !EMAIL_PATTERN.test(email)) {
    fieldErrors.email = "Enter a valid email address.";
  }

  if (Object.keys(fieldErrors).length > 0) {
    return createErrorState({
      fieldErrors,
      formError: null,
      correlationId: null,
    });
  }

  try {
    const body = await createMember({ email, name });
    revalidatePath("/members");
    return {
      status: "created",
      member: { id: body.id, email: body.email, name: body.name, role: body.role },
      tempPassword: body.temp_password,
    };
  } catch (err) {
    if (err instanceof AdminApiError) {
      return mapCreateMemberError(err);
    }
    return createErrorState({
      fieldErrors: {},
      formError: GENERIC_NETWORK_ERROR,
      correlationId: null,
    });
  }
}

function mapCreateMemberError(err: AdminApiError): CreateMemberErrorState {
  if (err.errorCode === "ADMIN_EMAIL_TAKEN") {
    return createErrorState({
      fieldErrors: { email: "That email is already in use." },
      formError: null,
      correlationId: err.correlationId || null,
    });
  }
  if (err.status === 403 || err.errorCode === "AUTHORIZATION_ERROR" || err.errorCode === "ROLE_NOT_PERMITTED") {
    return createErrorState({
      fieldErrors: {},
      formError: "You do not have permission to add team members.",
      correlationId: err.correlationId || null,
    });
  }
  if (err.status === 401) {
    return createErrorState({
      fieldErrors: {},
      formError: "Your session has expired. Please sign in again.",
      correlationId: err.correlationId || null,
    });
  }
  return createErrorState({
    fieldErrors: {},
    formError: `${err.message} (correlation ID: ${err.correlationId || "unknown"})`,
    correlationId: err.correlationId || null,
  });
}

// ---------------------------------------------------------------------------
// Toggle active / inactive
// ---------------------------------------------------------------------------

export interface ToggleActiveResult {
  status: "ok" | "error";
  member?: MemberSummary;
  message?: string;
  correlationId?: string;
}

/**
 * Activate/deactivate a member. Called from a confirm-before-deactivate
 * dialog in the client component (per the accessibility instructions: a
 * destructive-ish action affecting someone's access requires confirmation).
 * Confirmed, not optimistic: the returned `member` reflects the server's
 * response, and the table re-renders from `revalidatePath` on success.
 */
export async function toggleMemberActiveAction(
  userId: string,
  active: boolean
): Promise<ToggleActiveResult> {
  try {
    const member = await setMemberActive(userId, active);
    revalidatePath("/members");
    return { status: "ok", member };
  } catch (err) {
    if (err instanceof AdminApiError) {
      if (err.status === 404 || err.errorCode === "USER_NOT_FOUND") {
        return { status: "error", message: "That member could not be found.", correlationId: err.correlationId };
      }
      if (err.errorCode === "INVALID_TARGET_USER") {
        return {
          status: "error",
          message: "That member's access cannot be changed (you cannot target yourself or a non-agent).",
          correlationId: err.correlationId,
        };
      }
      if (err.status === 403) {
        return { status: "error", message: "You do not have permission to change member access.", correlationId: err.correlationId };
      }
      return { status: "error", message: err.message, correlationId: err.correlationId };
    }
    return { status: "error", message: GENERIC_NETWORK_ERROR, correlationId: "" };
  }
}

// ---------------------------------------------------------------------------
// Delete (inactive members only)
// ---------------------------------------------------------------------------

export interface DeleteMemberResult {
  status: "ok" | "error";
  message?: string;
  correlationId?: string;
}

/**
 * Permanently delete an already-deactivated member. Called from a
 * type-to-confirm dialog in the client component -- this is irreversible
 * (unlike Deactivate, there is no "Activate" button to undo it), so it
 * gets stronger confirmation than the toggle above.
 */
export async function deleteMemberAction(userId: string): Promise<DeleteMemberResult> {
  try {
    await deleteMember(userId);
    revalidatePath("/members");
    return { status: "ok" };
  } catch (err) {
    if (err instanceof AdminApiError) {
      if (err.status === 404 || err.errorCode === "USER_NOT_FOUND") {
        return { status: "error", message: "That member could not be found.", correlationId: err.correlationId };
      }
      if (err.errorCode === "USER_NOT_INACTIVE") {
        return {
          status: "error",
          message: "Deactivate this member before deleting them.",
          correlationId: err.correlationId,
        };
      }
      if (err.errorCode === "INVALID_TARGET_USER") {
        return {
          status: "error",
          message: "That member cannot be deleted (you cannot target yourself or a non-agent).",
          correlationId: err.correlationId,
        };
      }
      if (err.status === 403) {
        return { status: "error", message: "You do not have permission to delete team members.", correlationId: err.correlationId };
      }
      return { status: "error", message: err.message, correlationId: err.correlationId };
    }
    return { status: "error", message: GENERIC_NETWORK_ERROR, correlationId: "" };
  }
}

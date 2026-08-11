/**
 * Server-only data layer for the Team members screen (7b). Wraps the three
 * real endpoints in `services/api/src/api/admin/users_routes.py`:
 * `GET /admin/users` (list), `POST /admin/users` (create -- immediate,
 * hardcoded `role=CLIENT_AGENT`, returns a one-time `temp_password`), and
 * `PATCH /admin/users/{user_id}` (toggle `active`). Mirrors the typed-result
 * shape established by `lib/leads.ts`/`lib/clients.ts`: a backend error
 * always becomes a visible, honest state, never a fabricated row
 * (CLAUDE.md §3 "no silent fallbacks").
 *
 * SCOPE NOTE (locked by explicit user decision this session): there is no
 * pending-invite system, no third "VIEWER" role, no per-member open-lead-load
 * metric, and no auto-assignment setting server-side. This module exposes
 * only what the backend actually returns -- see `members-presentation.ts`
 * for the real 2-role badge mapping, and `app/(protected)/members/page.tsx`
 * for how the missing 7b elements are honestly stubbed/omitted.
 */
import "server-only";

import { adminApiFetch, AdminApiError } from "@/lib/api";

/** A single row of `GET /admin/users` -- mirrors `AdminUserResponse`
 * (users_routes.py:48-57) exactly. Never includes `password_hash` -- the
 * backend response is already leak-free by construction. */
export interface MemberSummary {
  id: string;
  tenantId: string | null;
  email: string;
  role: string;
  name: string | null;
  active: boolean;
  lastLoginAt: string | null;
}

interface AdminUserResponseBody {
  id: string;
  tenant_id: string | null;
  email: string;
  role: string;
  name: string | null;
  active: boolean;
  last_login_at: string | null;
}

function toMemberSummary(row: AdminUserResponseBody): MemberSummary {
  return {
    id: row.id,
    tenantId: row.tenant_id,
    email: row.email,
    role: row.role,
    name: row.name,
    active: row.active,
    lastLoginAt: row.last_login_at,
  };
}

export type MembersResult =
  | { status: "ok"; items: MemberSummary[] }
  | { status: "error"; message: string; correlationId: string };

/** List the caller's tenant's users (CLIENT_ADMIN + CLIENT_AGENT rows). */
export async function listMembers(): Promise<MembersResult> {
  return listMembersAtPath("/admin/users");
}

/** List a managed tenant's users for the PLATFORM_ADMIN per-client console. */
export async function listMembersForTenant(tenantId: string): Promise<MembersResult> {
  return listMembersAtPath(`/admin/tenants/${encodeURIComponent(tenantId)}/users`);
}

async function listMembersAtPath(path: string): Promise<MembersResult> {
  try {
    const response = await adminApiFetch(path);
    const body = (await response.json()) as AdminUserResponseBody[];
    return { status: "ok", items: body.map(toMemberSummary) };
  } catch (error) {
    if (error instanceof AdminApiError) {
      return { status: "error", message: mapErrorMessage(error), correlationId: error.correlationId };
    }
    return {
      status: "error",
      message: "Unable to reach the server. Please try again.",
      correlationId: "",
    };
  }
}

export interface CreateMemberInput {
  email: string;
  name?: string;
}

export interface CreateMemberResponseBody {
  id: string;
  tenant_id: string | null;
  email: string;
  role: string;
  name: string | null;
  active: boolean;
  last_login_at: string | null;
  temp_password: string;
}

/**
 * Call `POST /admin/users`. Creation is immediate (no invite/accept step) --
 * the new user is a `CLIENT_AGENT` with a server-generated `temp_password`
 * returned exactly once in the response body. Callers must never log the
 * response body (secrets hygiene, matching `tenants/new/actions.ts`).
 * Throws `AdminApiError` on any non-2xx (422 `ADMIN_EMAIL_TAKEN` on a
 * duplicate email).
 */
export async function createMember(input: CreateMemberInput): Promise<CreateMemberResponseBody> {
  const requestBody: Record<string, unknown> = { email: input.email };
  if (input.name) requestBody.name = input.name;

  const response = await adminApiFetch("/admin/users", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(requestBody),
  });
  return (await response.json()) as CreateMemberResponseBody;
}

/**
 * Call `PATCH /admin/users/{user_id}` to activate/deactivate a same-tenant
 * `CLIENT_AGENT`. Throws `AdminApiError` on any non-2xx (404
 * `USER_NOT_FOUND`, 422 `INVALID_TARGET_USER` for self-targeting or a
 * non-`CLIENT_AGENT` target).
 */
export async function setMemberActive(userId: string, active: boolean): Promise<MemberSummary> {
  const response = await adminApiFetch(`/admin/users/${encodeURIComponent(userId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ active }),
  });
  const body = (await response.json()) as AdminUserResponseBody;
  return toMemberSummary(body);
}

function mapErrorMessage(error: AdminApiError): string {
  if (error.status === 403 || error.errorCode === "ROLE_NOT_PERMITTED") {
    return "You do not have permission to view team members.";
  }
  if (error.status === 401) {
    return "Your session has expired. Please log in again.";
  }
  return `Something went wrong (${error.errorCode || "UNKNOWN_ERROR"}). Correlation ID: ${
    error.correlationId || "n/a"
  }.`;
}

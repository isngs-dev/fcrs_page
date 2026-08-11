"use server";

/**
 * Add-contact server action (SR-17 scope item 6). `CLIENT_ADMIN`-only per
 * D2 -- the "Add contact" form itself is only ever rendered for
 * `CLIENT_ADMIN` (the page hides it for `CLIENT_AGENT`), and this action
 * relies on the backend's `require_roles(Role.CLIENT_ADMIN)` on
 * `POST /admin/contacts` (contacts/admin_routes.py:197) as the actual
 * enforcement boundary -- matching every other action in this console
 * (`leads/actions.ts`'s `addLeadNote`), none of which re-check role
 * server-side; the API is the real gate (admin-web skill, CLAUDE.md §3).
 *
 * Zod-validated (courtesy pre-check, `lib/contacts-schema.ts`), then posts
 * to `POST /admin/contacts` with the exact `ConsentPayload` shape the
 * backend requires (D-lead-capture-crm: consent is a precondition, not
 * optional metadata -- omitting it 422s `CONSENT_REQUIRED` and persists
 * nothing).
 */
import { revalidatePath } from "next/cache";
import { AdminApiError, adminApiFetch } from "@/lib/api";
import { addContactFormSchema } from "@/lib/contacts-schema";

export interface AddContactIdleState {
  status: "idle";
}

export interface AddContactErrorState {
  status: "error";
  message: string;
}

export interface AddContactOkState {
  status: "ok";
  contactId: string;
}

export type AddContactState = AddContactIdleState | AddContactErrorState | AddContactOkState;

const GENERIC_NETWORK_ERROR = "Unable to reach the server. Please try again.";

export async function addContact(
  _prevState: AddContactState,
  formData: FormData
): Promise<AddContactState> {
  const raw = {
    name: formData.get("name")?.toString() ?? "",
    email: formData.get("email")?.toString() ?? "",
    phone: formData.get("phone")?.toString() ?? "",
    accountId: formData.get("accountId")?.toString() ?? "",
    consentGranted: formData.get("consentGranted")?.toString() ?? "",
    consentPurpose: formData.get("consentPurpose")?.toString() ?? "",
    consentText: formData.get("consentText")?.toString() ?? "",
  };

  const parsed = addContactFormSchema.safeParse(raw);
  if (!parsed.success) {
    const firstIssue = parsed.error.issues[0];
    return { status: "error", message: firstIssue?.message ?? "Invalid form input." };
  }

  const { data } = parsed;

  try {
    const response = await adminApiFetch("/admin/contacts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: data.name ?? null,
        email: data.email ?? null,
        phone: data.phone ?? null,
        account_id: data.accountId ?? null,
        consent: {
          granted: true,
          purpose: data.consentPurpose,
          text: data.consentText,
        },
      }),
    });
    const body = (await response.json()) as { contact_id: string };
    revalidatePath("/contacts");
    return { status: "ok", contactId: body.contact_id };
  } catch (error) {
    if (error instanceof AdminApiError) {
      return { status: "error", message: mapError(error) };
    }
    return { status: "error", message: GENERIC_NETWORK_ERROR };
  }
}

function mapError(error: AdminApiError): string {
  if (error.errorCode === "CONSENT_REQUIRED") {
    return "Consent to store contact information is required.";
  }
  if (error.errorCode === "INVALID_ACCOUNT") {
    return "The specified account does not exist in this tenant.";
  }
  if (error.status === 403 || error.errorCode === "ROLE_NOT_PERMITTED") {
    return "You do not have permission to add contacts.";
  }
  if (error.status === 401) {
    return "Your session has expired. Please log in again.";
  }
  return `${error.message} (correlation ID: ${error.correlationId || "unknown"}).`;
}

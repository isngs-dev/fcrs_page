/**
 * Shared (isomorphic) Zod schema for the "Add contact" form (SR-17 scope
 * item 6). Mirrors `ContactCreateRequest`
 * (services/api/src/api/contacts/admin_routes.py:51-65) field-for-field:
 * `name`/`email`/`phone`/`account_id` all optional, plus the mandatory
 * consent object (`granted`/`purpose`/`text`) the backend 422s
 * `CONSENT_REQUIRED` without (D-lead-capture-crm: consent is a precondition,
 * not a courtesy). This is a courtesy pre-check only -- the backend remains
 * authoritative and can still 422.
 */
import { z } from "zod";

export const addContactFormSchema = z.object({
  name: z
    .string()
    .trim()
    .max(200, "Name must be 200 characters or fewer.")
    .optional()
    .transform((value) => (value && value.length > 0 ? value : undefined)),
  email: z
    .string()
    .trim()
    .max(320, "Email must be 320 characters or fewer.")
    .optional()
    .transform((value) => (value && value.length > 0 ? value : undefined))
    .refine((value) => value === undefined || value.includes("@"), {
      message: "Email must contain @.",
    }),
  phone: z
    .string()
    .trim()
    .max(40, "Phone must be 40 characters or fewer.")
    .optional()
    .transform((value) => (value && value.length > 0 ? value : undefined)),
  accountId: z
    .string()
    .trim()
    .optional()
    .transform((value) => (value && value.length > 0 ? value : undefined)),
  consentGranted: z.literal("on", { message: "Consent is required to store contact information." }),
  consentPurpose: z.string().trim().min(1, "Consent purpose is required.").max(200),
  consentText: z.string().trim().min(1, "Consent text is required.").max(2000),
});

export type AddContactFormInput = z.input<typeof addContactFormSchema>;
export type AddContactFormParsed = z.output<typeof addContactFormSchema>;

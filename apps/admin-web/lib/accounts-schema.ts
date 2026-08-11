/**
 * Shared (isomorphic) Zod schema for the "Add account" form (SR-17 scope
 * item 6). Mirrors `AccountCreateRequest`
 * (services/api/src/api/accounts/admin_routes.py:33-44): `name` required
 * non-blank, `domain` optional. Courtesy pre-check only -- the backend
 * remains authoritative.
 */
import { z } from "zod";

export const addAccountFormSchema = z.object({
  name: z
    .string()
    .trim()
    .min(1, "Name is required.")
    .max(200, "Name must be 200 characters or fewer."),
  domain: z
    .string()
    .trim()
    .max(253, "Domain must be 253 characters or fewer.")
    .optional()
    .transform((value) => (value && value.length > 0 ? value : undefined)),
});

export type AddAccountFormInput = z.input<typeof addAccountFormSchema>;
export type AddAccountFormParsed = z.output<typeof addAccountFormSchema>;

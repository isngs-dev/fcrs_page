import { z } from "zod";

// The ONE shared schema governing the estimate-request endpoint. Any future
// second entry point must submit this identical field set — see CLAUDE.md
// "Field parity across forms that feed one record".

export const SERVICE_OPTIONS = [
  "Residential Roofing",
  "Commercial Roofing",
  "Emergency Repair",
];

export const STATE_OPTIONS = ["Alabama", "Georgia"];

// Human-facing messages (never zod's raw issue codes) so the route layer can
// build the flat `{ field: message }` map required by the response contract.
const requiredString = (label) =>
  z
    .string({ required_error: `${label} is required.`, invalid_type_error: `${label} is required.` })
    .trim()
    .min(1, `${label} is required.`);

// Optional `date` (datetime-local string): empty string normalizes to
// absent rather than failing validation.
const optionalDate = z.preprocess((val) => {
  if (typeof val === "string" && val.trim() === "") return undefined;
  return val;
}, z.string().trim().optional());

// Optional `notes`: empty string is kept as a harmless empty optional value.
const optionalNotes = z.preprocess((val) => {
  if (val === undefined || val === null) return undefined;
  return val;
}, z.string().optional());

// .strip() is zod's default object behavior: unknown keys are silently
// dropped from the parsed result rather than rejected or echoed back.
export const estimateRequestSchema = z
  .object({
    name: requiredString("Full name"),
    phone: requiredString("Phone number"),
    email: requiredString("Email address")
      .pipe(z.string().email("Enter a valid email address."))
      .transform((val) => val.trim().toLowerCase()),
    service: z.enum(SERVICE_OPTIONS, {
      errorMap: () => ({ message: "Select a service." }),
    }),
    state: z.enum(STATE_OPTIONS, {
      errorMap: () => ({ message: "Select a state." }),
    }),
    zip: requiredString("Zip code"),
    date: optionalDate,
    notes: optionalNotes,
  })
  .strip();

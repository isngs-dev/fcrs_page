import { describe, expect, it } from "vitest";
import { addContactFormSchema } from "@/lib/contacts-schema";

const validConsent = {
  consentGranted: "on" as const,
  consentPurpose: "CRM record",
  consentText: "Agreed to store contact info",
};

describe("addContactFormSchema", () => {
  it("accepts a minimal valid submission with consent granted", () => {
    const result = addContactFormSchema.safeParse({ ...validConsent });
    expect(result.success).toBe(true);
  });

  it("rejects a submission without consent granted (D-lead-capture-crm: consent is mandatory)", () => {
    const result = addContactFormSchema.safeParse({
      consentPurpose: "CRM record",
      consentText: "Agreed",
    });
    expect(result.success).toBe(false);
  });

  it("rejects an invalid email (no @)", () => {
    const result = addContactFormSchema.safeParse({ ...validConsent, email: "not-an-email" });
    expect(result.success).toBe(false);
  });

  it("accepts a valid email", () => {
    const result = addContactFormSchema.safeParse({ ...validConsent, email: "a@example.com" });
    expect(result.success).toBe(true);
  });

  it("blank optional fields transform to undefined, not empty string", () => {
    const result = addContactFormSchema.safeParse({ ...validConsent, name: "", phone: "" });
    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.name).toBeUndefined();
      expect(result.data.phone).toBeUndefined();
    }
  });

  it("rejects a blank consent purpose", () => {
    const result = addContactFormSchema.safeParse({ ...validConsent, consentPurpose: "" });
    expect(result.success).toBe(false);
  });

  it("rejects a blank consent text", () => {
    const result = addContactFormSchema.safeParse({ ...validConsent, consentText: "" });
    expect(result.success).toBe(false);
  });
});

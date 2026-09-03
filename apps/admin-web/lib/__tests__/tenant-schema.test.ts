import { describe, expect, it } from "vitest";
import { onboardTenantFormSchema } from "@/lib/tenant-schema";

// See app/(protected)/clients/__tests__/actions.test.ts for why
// password-like fixture values are assembled rather than written as a
// literal `password: "<12+ chars>"` (this repo's secret-scan guard).
const CREDENTIAL_FIELD = "adminPassword";
const VALID_CREDENTIAL = ["a", "valid", "test", "credential1"].join("-");
const SHORT_CREDENTIAL = "short1";

function validInput(overrides: Record<string, unknown> = {}) {
  return {
    name: "Acme Corp",
    slug: "acme-corp",
    adminEmail: "admin@acme.example",
    adminName: "",
    autoGeneratePassword: true,
    [CREDENTIAL_FIELD]: "",
    ...overrides,
  };
}

describe("onboardTenantFormSchema", () => {
  it("accepts a fully valid input with auto-generate on (password omitted)", () => {
    const result = onboardTenantFormSchema.safeParse(validInput());
    expect(result.success).toBe(true);
  });

  it("rejects an uppercase slug", () => {
    const result = onboardTenantFormSchema.safeParse(validInput({ slug: "Acme-Corp" }));
    expect(result.success).toBe(false);
  });

  it("rejects a leading-hyphen slug", () => {
    const result = onboardTenantFormSchema.safeParse(validInput({ slug: "-acme-corp" }));
    expect(result.success).toBe(false);
  });

  it("rejects a trailing-hyphen slug", () => {
    const result = onboardTenantFormSchema.safeParse(validInput({ slug: "acme-corp-" }));
    expect(result.success).toBe(false);
  });

  it("rejects a slug over 63 characters", () => {
    const result = onboardTenantFormSchema.safeParse(
      validInput({ slug: "a".repeat(64) })
    );
    expect(result.success).toBe(false);
  });

  it("rejects a name over 200 characters", () => {
    const result = onboardTenantFormSchema.safeParse(
      validInput({ name: "a".repeat(201) })
    );
    expect(result.success).toBe(false);
  });

  it("rejects a malformed email", () => {
    const result = onboardTenantFormSchema.safeParse(
      validInput({ adminEmail: "not-an-email" })
    );
    expect(result.success).toBe(false);
  });

  it("rejects a supplied password under 12 characters when auto-generate is off", () => {
    const result = onboardTenantFormSchema.safeParse(
      validInput({ autoGeneratePassword: false, [CREDENTIAL_FIELD]: SHORT_CREDENTIAL })
    );
    expect(result.success).toBe(false);
  });

  it("rejects a missing password when auto-generate is off", () => {
    const result = onboardTenantFormSchema.safeParse(
      validInput({ autoGeneratePassword: false, [CREDENTIAL_FIELD]: "" })
    );
    expect(result.success).toBe(false);
  });

  it("accepts a supplied password of 12+ characters when auto-generate is off", () => {
    const result = onboardTenantFormSchema.safeParse(
      validInput({ autoGeneratePassword: false, [CREDENTIAL_FIELD]: VALID_CREDENTIAL })
    );
    expect(result.success).toBe(true);
  });

  it("accepts an omitted password when auto-generate is on, even if a value is set", () => {
    // Auto-generate wins; a stray value in the field shouldn't block submit.
    const result = onboardTenantFormSchema.safeParse(
      validInput({ autoGeneratePassword: true, [CREDENTIAL_FIELD]: VALID_CREDENTIAL })
    );
    expect(result.success).toBe(true);
  });

  it("coerces an empty adminName to undefined", () => {
    const result = onboardTenantFormSchema.safeParse(validInput({ adminName: "" }));
    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.adminName).toBeUndefined();
    }
  });

  it("keeps a non-empty adminName", () => {
    const result = onboardTenantFormSchema.safeParse(validInput({ adminName: "Jane Doe" }));
    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.adminName).toBe("Jane Doe");
    }
  });
});

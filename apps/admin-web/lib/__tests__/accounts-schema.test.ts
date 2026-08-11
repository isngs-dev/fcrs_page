import { describe, expect, it } from "vitest";
import { addAccountFormSchema } from "@/lib/accounts-schema";

describe("addAccountFormSchema", () => {
  it("accepts a minimal valid submission (name only)", () => {
    const result = addAccountFormSchema.safeParse({ name: "Acme Corp" });
    expect(result.success).toBe(true);
  });

  it("rejects a blank name", () => {
    const result = addAccountFormSchema.safeParse({ name: "" });
    expect(result.success).toBe(false);
  });

  it("rejects a whitespace-only name", () => {
    const result = addAccountFormSchema.safeParse({ name: "   " });
    expect(result.success).toBe(false);
  });

  it("accepts an optional domain", () => {
    const result = addAccountFormSchema.safeParse({ name: "Acme", domain: "acme.example.com" });
    expect(result.success).toBe(true);
    if (result.success) expect(result.data.domain).toBe("acme.example.com");
  });

  it("a blank domain transforms to undefined", () => {
    const result = addAccountFormSchema.safeParse({ name: "Acme", domain: "" });
    expect(result.success).toBe(true);
    if (result.success) expect(result.data.domain).toBeUndefined();
  });
});

import { describe, it, expect } from "vitest";
import { estimateRequestSchema } from "../src/schema/estimate-request.js";

const validSubmission = {
  name: "Jane Smith",
  phone: "(989) 843-4628",
  email: "jane@example.com",
  service: "Residential Roofing",
  state: "Alabama",
  zip: "35203",
};

describe("estimateRequestSchema", () => {
  it("accepts a fully valid submission with only required fields", () => {
    const result = estimateRequestSchema.safeParse(validSubmission);
    expect(result.success).toBe(true);
  });

  it("accepts a fully valid submission including optional fields", () => {
    const result = estimateRequestSchema.safeParse({
      ...validSubmission,
      date: "2026-08-01T10:00",
      notes: "Leak near the chimney.",
    });
    expect(result.success).toBe(true);
  });

  for (const field of ["name", "phone", "email", "service", "state", "zip"]) {
    it(`rejects a submission missing required field "${field}"`, () => {
      const submission = { ...validSubmission };
      delete submission[field];
      const result = estimateRequestSchema.safeParse(submission);
      expect(result.success).toBe(false);
      const keys = result.error.issues.map((issue) => issue.path[0]);
      expect(keys).toContain(field);
    });
  }

  it("rejects an invalid email", () => {
    const result = estimateRequestSchema.safeParse({ ...validSubmission, email: "not-an-email" });
    expect(result.success).toBe(false);
    expect(result.error.issues.some((i) => i.path[0] === "email")).toBe(true);
  });

  it("trims and lowercases email for dedupe comparison", () => {
    const result = estimateRequestSchema.safeParse({ ...validSubmission, email: "  Jane@EXAMPLE.com  " });
    expect(result.success).toBe(true);
    expect(result.data.email).toBe("jane@example.com");
  });

  it("rejects a service value outside the exact enum", () => {
    const result = estimateRequestSchema.safeParse({ ...validSubmission, service: "Gutter Cleaning" });
    expect(result.success).toBe(false);
    expect(result.error.issues.some((i) => i.path[0] === "service")).toBe(true);
  });

  it("rejects an empty-string service (the disabled placeholder option)", () => {
    const result = estimateRequestSchema.safeParse({ ...validSubmission, service: "" });
    expect(result.success).toBe(false);
    expect(result.error.issues.some((i) => i.path[0] === "service")).toBe(true);
  });

  it("rejects a state value outside the exact enum", () => {
    const result = estimateRequestSchema.safeParse({ ...validSubmission, state: "Florida" });
    expect(result.success).toBe(false);
    expect(result.error.issues.some((i) => i.path[0] === "state")).toBe(true);
  });

  it("rejects an empty-string state (the disabled placeholder option)", () => {
    const result = estimateRequestSchema.safeParse({ ...validSubmission, state: "" });
    expect(result.success).toBe(false);
    expect(result.error.issues.some((i) => i.path[0] === "state")).toBe(true);
  });

  it("treats an empty-string date as absent", () => {
    const result = estimateRequestSchema.safeParse({ ...validSubmission, date: "" });
    expect(result.success).toBe(true);
    expect(result.data.date).toBeUndefined();
  });

  it("is valid when optional date and notes are absent", () => {
    const result = estimateRequestSchema.safeParse(validSubmission);
    expect(result.success).toBe(true);
    expect(result.data.date).toBeUndefined();
    expect(result.data.notes).toBeUndefined();
  });

  it("strips unknown keys rather than rejecting or echoing them back", () => {
    const result = estimateRequestSchema.safeParse({
      ...validSubmission,
      source: "footer-form",
      admin: true,
    });
    expect(result.success).toBe(true);
    expect(result.data).not.toHaveProperty("source");
    expect(result.data).not.toHaveProperty("admin");
  });
});

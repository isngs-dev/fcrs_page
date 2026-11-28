import { describe, it, expect } from "vitest";
import {
  FIELD_DEFS,
  REQUIRED_FIELD_NAMES,
  SERVICE_OPTIONS,
  STATE_OPTIONS,
  validateEstimateForm,
  isValid,
  toIntakePayload,
} from "./estimate-form-schema.js";

const validValues = {
  name: "Jane Smith",
  phone: "(989) 843-4628",
  email: "jane@example.com",
  service: "Residential Roofing",
  state: "Alabama",
  zip: "35203",
  date: "",
  notes: "",
};

describe("shared field schema mirror", () => {
  it("declares exactly the six required fields per CLAUDE.md field table", () => {
    expect(REQUIRED_FIELD_NAMES.sort()).toEqual(
      ["name", "phone", "email", "service", "state", "zip"].sort()
    );
  });

  it("declares exactly the two optional fields", () => {
    const optional = FIELD_DEFS.filter((f) => !f.required).map((f) => f.name);
    expect(optional.sort()).toEqual(["date", "notes"].sort());
  });

  it("has exactly eight fields total, matching the backend schema", () => {
    expect(FIELD_DEFS).toHaveLength(8);
  });

  it("mirrors the service enum exactly, in order", () => {
    expect(SERVICE_OPTIONS).toEqual([
      "Residential Roofing",
      "Commercial Roofing",
      "Emergency Repair",
    ]);
  });

  it("mirrors the state enum exactly, in order", () => {
    expect(STATE_OPTIONS).toEqual(["Alabama", "Georgia"]);
  });
});

describe("validateEstimateForm - required-field checks", () => {
  it("passes with all required fields present and valid", () => {
    const errors = validateEstimateForm(validValues);
    expect(isValid(errors)).toBe(true);
  });

  it.each(["name", "phone", "email", "service", "state", "zip"])(
    "flags %s as required when blank",
    (field) => {
      const errors = validateEstimateForm({ ...validValues, [field]: "" });
      expect(errors[field]).toBeTruthy();
    }
  );

  it.each(["name", "phone", "email", "service", "state", "zip"])(
    "flags %s as required when whitespace-only",
    (field) => {
      const errors = validateEstimateForm({ ...validValues, [field]: "   " });
      expect(errors[field]).toBeTruthy();
    }
  );

  it("does not require date", () => {
    const errors = validateEstimateForm({ ...validValues, date: undefined });
    expect(errors.date).toBeUndefined();
  });

  it("does not require notes", () => {
    const errors = validateEstimateForm({ ...validValues, notes: undefined });
    expect(errors.notes).toBeUndefined();
  });
});

describe("validateEstimateForm - email format", () => {
  it.each([
    "not-an-email",
    "missing-domain@",
    "@missing-local.com",
    "spaces in@email.com",
    "no-at-sign.com",
  ])("rejects malformed email %s", (bad) => {
    const errors = validateEstimateForm({ ...validValues, email: bad });
    expect(errors.email).toBeTruthy();
  });

  it.each([
    "jane@example.com",
    "jane.smith+roof@sub.example.co",
    "j@e.co",
  ])("accepts well-formed email %s", (good) => {
    const errors = validateEstimateForm({ ...validValues, email: good });
    expect(errors.email).toBeUndefined();
  });
});

describe("validateEstimateForm - enum membership", () => {
  it("rejects a service value outside the enum", () => {
    const errors = validateEstimateForm({
      ...validValues,
      service: "Other",
    });
    expect(errors.service).toBeTruthy();
  });

  it("rejects a state value outside the enum", () => {
    const errors = validateEstimateForm({
      ...validValues,
      state: "Florida",
    });
    expect(errors.state).toBeTruthy();
  });

  it.each(SERVICE_OPTIONS)("accepts service enum member %s", (svc) => {
    const errors = validateEstimateForm({ ...validValues, service: svc });
    expect(errors.service).toBeUndefined();
  });

  it.each(STATE_OPTIONS)("accepts state enum member %s", (st) => {
    const errors = validateEstimateForm({ ...validValues, state: st });
    expect(errors.state).toBeUndefined();
  });
});

describe("isValid", () => {
  it("returns true for an empty error map", () => {
    expect(isValid({})).toBe(true);
  });

  it("returns false when any error is present", () => {
    expect(isValid({ email: "bad" })).toBe(false);
  });
});

describe("toIntakePayload - what the platform's form intake receives", () => {
  it("sends the contact fields, labelled answers and a yes when the box is ticked", () => {
    const payload = toIntakePayload(
      { ...validValues, date: "2026-12-01T10:00", notes: "Leak over the garage" },
      { callConsent: true, timezone: "America/Chicago", countryCode: "+1" }
    );
    expect(payload).toEqual({
      form: "ISN Roofing estimate",
      name: "Jane Smith",
      phone: "(989) 843-4628",
      country_code: "+1",
      email: "jane@example.com",
      "Service Needed": "Residential Roofing",
      State: "Alabama",
      "Zip Code": "35203",
      "Preferred Date / Time": "2026-12-01T10:00",
      Message: "Leak over the garage",
      call_consent: "yes",
      timezone: "America/Chicago",
    });
  });

  it("says no to calls when the box is left unticked, and omits an unknown timezone", () => {
    const payload = toIntakePayload(validValues, {
      callConsent: false,
      timezone: "",
      countryCode: "+91",
    });
    expect(payload.country_code).toBe("+91");
    expect(payload.call_consent).toBe("no");
    expect("timezone" in payload).toBe(false);
  });
});

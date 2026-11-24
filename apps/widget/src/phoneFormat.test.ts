import { describe, expect, it } from "vitest";

import { formatPhoneInput, formatUsPhoneInput, splitDialCode, withDialCode } from "./phoneFormat";

describe("formatUsPhoneInput", () => {
  it("returns an empty string for empty input", () => {
    expect(formatUsPhoneInput("")).toBe("");
  });

  it("formats progressively as digits are typed", () => {
    expect(formatUsPhoneInput("7")).toBe("(7");
    expect(formatUsPhoneInput("78")).toBe("(78");
    expect(formatUsPhoneInput("786")).toBe("(786");
    expect(formatUsPhoneInput("7867")).toBe("(786) 7");
    expect(formatUsPhoneInput("786775")).toBe("(786) 775");
    expect(formatUsPhoneInput("7867756")).toBe("(786) 775-6");
    expect(formatUsPhoneInput("7867756768")).toBe("(786) 775-6768");
  });

  it("caps at 10 digits -- an 11th+ digit is silently dropped", () => {
    expect(formatUsPhoneInput("78677567689999")).toBe("(786) 775-6768");
  });

  it("strips letters and symbols -- only digits ever reach the formatted output", () => {
    expect(formatUsPhoneInput("abc")).toBe("");
    expect(formatUsPhoneInput("(786) 775-6768")).toBe("(786) 775-6768");
    expect(formatUsPhoneInput("786-775-6768 ext. 12")).toBe("(786) 775-6768");
    expect(formatUsPhoneInput("call me maybe")).toBe("");
  });

  it("re-formatting an already-formatted value is idempotent", () => {
    const once = formatUsPhoneInput("7867756768");
    expect(formatUsPhoneInput(once)).toBe(once);
  });
});

// Live 2026-11-20: an Indian number on the booking card was cut to 10 digits in US format.
describe("formatUsPhoneInput with a country code", () => {
  it("leaves an international number as typed", () => {
    expect(formatUsPhoneInput("+91 98765 43210")).toBe("+91 98765 43210");
    expect(formatUsPhoneInput("+919876543210")).toBe("+919876543210");
  });
  it("drops letters but keeps the plus, digits, spaces and dashes", () => {
    expect(formatUsPhoneInput("+44 (20) 7946-0958x")).toBe("+44 (20) 7946-0958");
  });
});

describe("the country-code picker (owner, 2026-11-24)", () => {
  it("formats an Indian number as 5 + 5 digits, capped at 10", () => {
    expect(formatPhoneInput("98765", "+91")).toBe("98765");
    expect(formatPhoneInput("987654321099", "+91")).toBe("98765 43210");
  });

  it("formats a US number as before", () => {
    expect(formatPhoneInput("7867756768", "+1")).toBe("(786) 775-6768");
  });

  it("puts +91 in front of an Indian number, and leaves a US one as typed", () => {
    expect(withDialCode("98765 43210", "+91")).toBe("+919876543210");
    expect(withDialCode("(786) 775-6768", "+1")).toBe("(786) 775-6768");
    expect(withDialCode("+44 20 7946 0958", "+91")).toBe("+44 20 7946 0958");
    expect(withDialCode("  ", "+91")).toBe("");
  });

  it("splits a known number back into the picker and the field", () => {
    expect(splitDialCode("+919876543210")).toEqual({ dial: "+91", number: "98765 43210" });
    expect(splitDialCode("+17867756768")).toEqual({ dial: "+1", number: "(786) 775-6768" });
    expect(splitDialCode("(786) 775-6768")).toEqual({ dial: "+1", number: "(786) 775-6768" });
  });
});

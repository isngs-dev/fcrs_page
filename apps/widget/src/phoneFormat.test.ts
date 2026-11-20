import { describe, expect, it } from "vitest";

import { formatUsPhoneInput } from "./phoneFormat";

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

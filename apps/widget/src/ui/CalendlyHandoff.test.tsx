import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { WidgetConfig } from "../config";
import type { LeadResult } from "../lead";
import type { AvailabilitySummary, PostHandoffIntentResult } from "../schedule";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const submitLeadMock = vi.fn<(config: WidgetConfig, input: unknown) => Promise<LeadResult>>();
const postHandoffIntentMock = vi.fn<(config: WidgetConfig, input: unknown) => Promise<PostHandoffIntentResult>>();

vi.mock("../lead", async () => {
  const actual = await vi.importActual<typeof import("../lead")>("../lead");
  return {
    ...actual,
    submitLead: (config: WidgetConfig, input: unknown) => submitLeadMock(config, input),
  };
});

vi.mock("../schedule", async () => {
  const actual = await vi.importActual<typeof import("../schedule")>("../schedule");
  return {
    ...actual,
    postHandoffIntent: (config: WidgetConfig, input: unknown) => postHandoffIntentMock(config, input),
  };
});

import { CalendlyHandoff } from "./CalendlyHandoff";
import { SCHEDULE_CONSENT_PURPOSE, SCHEDULE_CONSENT_TEXT } from "../schedule";

const baseConfig: WidgetConfig = {
  clientKey: "pk_test_123",
  apiBase: "http://localhost:8000",
  mountSelector: null,
  debug: false,
  position: "right",
};

const baseSummary: AvailabilitySummary = {
  action: "calendly_handoff",
  timezone: "UTC",
  days: [],
  transitionMessage: "Happy to connect you with a sales rep.",
  existingBooking: null,
  schedulingUrl: "https://calendly.com/it-isngs/30min",
};

let container: HTMLDivElement;
let root: Root;

function flush(): Promise<void> {
  return act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  submitLeadMock.mockReset();
  postHandoffIntentMock.mockReset();
});

afterEach(() => {
  act(() => {
    root.unmount();
  });
  container.remove();
  vi.restoreAllMocks();
});

function getEmailInput(): HTMLInputElement {
  const input = container.querySelector<HTMLInputElement>("#cw-sched-handoff-email");
  if (!input) throw new Error("email input not found");
  return input;
}

function getNameInput(): HTMLInputElement {
  const input = container.querySelector<HTMLInputElement>("#cw-sched-handoff-name");
  if (!input) throw new Error("name input not found");
  return input;
}

function getPhoneInput(): HTMLInputElement {
  const input = container.querySelector<HTMLInputElement>("#cw-sched-handoff-phone");
  if (!input) throw new Error("phone input not found");
  return input;
}

function getContinueButton(): HTMLButtonElement {
  const button = container.querySelector<HTMLButtonElement>(".cw-sched-handoff-continue-button");
  if (!button) throw new Error("continue button not found");
  return button;
}

// React tracks input values via the native element's own property
// descriptor; a plain `input.value = text` write is invisible to React's
// synthetic event system in jsdom (same trick LeadForm.test.tsx uses).
function setNativeInputValue(input: HTMLInputElement, text: string): void {
  // eslint-disable-next-line @typescript-eslint/unbound-method
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value")!.set!;
  Reflect.apply(setter, input, [text]);
  input.dispatchEvent(new Event("input", { bubbles: true }));
}

function fillRequiredFields(): void {
  act(() => {
    setNativeInputValue(getEmailInput(), "ada@example.com");
    setNativeInputValue(getNameInput(), "Ada Lovelace");
  });
}

const okHandoff: PostHandoffIntentResult = { ok: true, recorded: true };
const okLead: LeadResult = { ok: true, lead: { leadId: "lead-1", status: "new" } };

describe("CalendlyHandoff", () => {
  it("renders visible, labeled, placeholder-carrying email/name/phone inputs", () => {
    act(() => {
      root.render(<CalendlyHandoff config={baseConfig} summary={baseSummary} />);
    });

    const email = getEmailInput();
    expect(email.className).toContain("cw-lead-input");
    expect(email.placeholder).toBe("you@example.com");

    const name = getNameInput();
    expect(name.className).toContain("cw-lead-input");
    expect(name.placeholder.length).toBeGreaterThan(0);

    const phone = getPhoneInput();
    expect(phone.className).toContain("cw-lead-input");
    expect(phone.placeholder.length).toBeGreaterThan(0);
    expect(phone.required).toBe(false);
  });

  it("every field's label is associated to its input via htmlFor/id", () => {
    act(() => {
      root.render(<CalendlyHandoff config={baseConfig} summary={baseSummary} />);
    });

    for (const id of ["cw-sched-handoff-email", "cw-sched-handoff-name", "cw-sched-handoff-phone"]) {
      const label = container.querySelector<HTMLLabelElement>(`label[for="${id}"]`);
      expect(label, `label for #${id}`).not.toBeNull();
      expect(container.querySelector(`#${id}`)).not.toBeNull();
    }
  });

  it("Continue stays disabled until email AND name are filled; phone stays optional", () => {
    act(() => {
      root.render(<CalendlyHandoff config={baseConfig} summary={baseSummary} />);
    });

    expect(getContinueButton().disabled).toBe(true);

    act(() => {
      setNativeInputValue(getEmailInput(), "ada@example.com");
    });
    expect(getContinueButton().disabled).toBe(true);

    act(() => {
      setNativeInputValue(getNameInput(), "Ada Lovelace");
    });
    expect(getContinueButton().disabled).toBe(false);
  });

  it("submitting calls postHandoffIntent AND submitLead concurrently with matching details", async () => {
    postHandoffIntentMock.mockResolvedValueOnce(okHandoff);
    submitLeadMock.mockResolvedValueOnce(okLead);

    act(() => {
      root.render(<CalendlyHandoff config={baseConfig} summary={baseSummary} />);
    });

    fillRequiredFields();
    act(() => {
      // The phone field live-formats as a US number (formatUsPhoneInput) --
      // typed digits, submitted formatted.
      setNativeInputValue(getPhoneInput(), "5551234567");
    });

    act(() => {
      getContinueButton().click();
    });
    await flush();

    expect(postHandoffIntentMock).toHaveBeenCalledTimes(1);
    expect(postHandoffIntentMock).toHaveBeenCalledWith(baseConfig, { email: "ada@example.com" });

    expect(submitLeadMock).toHaveBeenCalledTimes(1);
    expect(submitLeadMock).toHaveBeenCalledWith(
      baseConfig,
      expect.objectContaining({
        name: "Ada Lovelace",
        email: "ada@example.com",
        phone: "(555) 123-4567",
        consent: { granted: true, purpose: SCHEDULE_CONSENT_PURPOSE, text: SCHEDULE_CONSENT_TEXT },
      }),
    );
  });

  it("the phone field strips letters/symbols as typed and caps at 10 digits", () => {
    act(() => {
      root.render(<CalendlyHandoff config={baseConfig} summary={baseSummary} />);
    });

    const phoneInput = getPhoneInput();

    act(() => {
      setNativeInputValue(phoneInput, "call me maybe");
    });
    expect(phoneInput.value).toBe("");

    act(() => {
      setNativeInputValue(phoneInput, "78677567689999");
    });
    expect(phoneInput.value).toBe("(786) 775-6768");
  });

  it("omitted phone -> submitLead is called WITHOUT a phone key", async () => {
    postHandoffIntentMock.mockResolvedValueOnce(okHandoff);
    submitLeadMock.mockResolvedValueOnce(okLead);

    act(() => {
      root.render(<CalendlyHandoff config={baseConfig} summary={baseSummary} />);
    });

    fillRequiredFields();
    act(() => {
      getContinueButton().click();
    });
    await flush();

    const call = submitLeadMock.mock.calls[0]![1] as Record<string, unknown>;
    expect("phone" in call).toBe(false);
  });

  it("on full success, reveals the link-out button (never before postHandoffIntent succeeds)", async () => {
    postHandoffIntentMock.mockResolvedValueOnce(okHandoff);
    submitLeadMock.mockResolvedValueOnce(okLead);

    act(() => {
      root.render(<CalendlyHandoff config={baseConfig} summary={baseSummary} />);
    });

    fillRequiredFields();
    act(() => {
      getContinueButton().click();
    });
    await flush();

    const linkButton = container.querySelector(".cw-sched-handoff-link-button");
    expect(linkButton).not.toBeNull();
    expect(linkButton?.textContent).toMatch(/open scheduling page/i);
  });

  it("postHandoffIntent failure shows an honest error and NEVER reveals the link-out button, even if submitLead succeeded", async () => {
    postHandoffIntentMock.mockResolvedValueOnce({
      ok: false,
      error: {
        type: "SCHEDULE_ERROR",
        errorCode: "RATE_LIMITED",
        message: "Too many requests.",
        correlationId: "corr-111",
        status: 429,
        retryAfterSeconds: null,
      },
    });
    submitLeadMock.mockResolvedValueOnce(okLead);
    const consoleErrorSpy = vi.spyOn(console, "error").mockImplementation(() => {});

    act(() => {
      root.render(<CalendlyHandoff config={baseConfig} summary={baseSummary} />);
    });

    fillRequiredFields();
    act(() => {
      getContinueButton().click();
    });
    await flush();

    expect(container.querySelector(".cw-sched-handoff-link-button")).toBeNull();
    const errorLine = container.querySelector(".cw-sched-error");
    expect(errorLine).not.toBeNull();
    expect(errorLine?.getAttribute("role")).toBe("alert");

    // Form re-enabled for manual retry.
    expect(getEmailInput().disabled).toBe(false);

    const loggedText = consoleErrorSpy.mock.calls.map((c) => (c as unknown[]).join(" ")).join("\n");
    expect(loggedText).toContain("RATE_LIMITED");
    expect(loggedText).toContain("corr-111");
    expect(loggedText).not.toContain("ada@example.com");
    expect(loggedText).not.toContain("Ada Lovelace");
  });

  it("submitLead failure is best-effort: logs but still reveals the link-out button", async () => {
    postHandoffIntentMock.mockResolvedValueOnce(okHandoff);
    submitLeadMock.mockResolvedValueOnce({
      ok: false,
      error: {
        type: "LEAD_ERROR",
        errorCode: "VALIDATION_ERROR",
        message: "Invalid lead payload.",
        correlationId: "corr-222",
        status: 422,
        retryAfterSeconds: null,
      },
    });
    const consoleErrorSpy = vi.spyOn(console, "error").mockImplementation(() => {});

    act(() => {
      root.render(<CalendlyHandoff config={baseConfig} summary={baseSummary} />);
    });

    fillRequiredFields();
    act(() => {
      getContinueButton().click();
    });
    await flush();

    // The booking hand-off is never blocked by a secondary CRM write failing.
    const linkButton = container.querySelector(".cw-sched-handoff-link-button");
    expect(linkButton).not.toBeNull();

    const loggedText = consoleErrorSpy.mock.calls.map((c) => (c as unknown[]).join(" ")).join("\n");
    expect(loggedText).toContain("submitLead failed");
    expect(loggedText).toContain("VALIDATION_ERROR");
    expect(loggedText).toContain("corr-222");
    expect(loggedText).not.toContain("ada@example.com");
    expect(loggedText).not.toContain("Ada Lovelace");
  });
});

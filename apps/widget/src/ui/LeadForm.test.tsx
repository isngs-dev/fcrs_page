import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { WidgetConfig } from "../config";
import type { LeadResult } from "../lead";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const submitLeadMock = vi.fn<(config: WidgetConfig, input: unknown) => Promise<LeadResult>>();

vi.mock("../lead", async () => {
  const actual = await vi.importActual<typeof import("../lead")>("../lead");
  return {
    ...actual,
    submitLead: (config: WidgetConfig, input: unknown) => submitLeadMock(config, input),
  };
});

import { LeadForm } from "./LeadForm";
import { CONSENT_PURPOSE, CONSENT_TEXT } from "../lead";

const baseConfig: WidgetConfig = {
  clientKey: "pk_test_123",
  apiBase: "http://localhost:8000",
  mountSelector: null,
  debug: false,
  position: "right",
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
});

afterEach(() => {
  act(() => {
    root.unmount();
  });
  container.remove();
  vi.restoreAllMocks();
});

function getNameInput(): HTMLInputElement {
  const input = container.querySelector<HTMLInputElement>("#cw-lead-name");
  if (!input) throw new Error("name input not found");
  return input;
}

function getEmailInput(): HTMLInputElement {
  const input = container.querySelector<HTMLInputElement>("#cw-lead-email");
  if (!input) throw new Error("email input not found");
  return input;
}

function getConsentCheckbox(): HTMLInputElement {
  const input = container.querySelector<HTMLInputElement>("#cw-lead-consent");
  if (!input) throw new Error("consent checkbox not found");
  return input;
}

function getSubmitButton(): HTMLButtonElement {
  const button = container.querySelector<HTMLButtonElement>(".cw-lead-submit");
  if (!button) throw new Error("submit button not found");
  return button;
}

// React tracks input values via the native element's own property
// descriptor; a plain `input.value = text` write is invisible to React's
// synthetic event system in jsdom. Go through the native setter (same
// trick ChatWidget.test.tsx uses) so the subsequent "input" event registers.
function setNativeInputValue(input: HTMLInputElement, text: string): void {
  // eslint-disable-next-line @typescript-eslint/unbound-method
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value")!.set!;
  Reflect.apply(setter, input, [text]);
  input.dispatchEvent(new Event("input", { bubbles: true }));
}

function fillRequiredFields(): void {
  act(() => {
    setNativeInputValue(getNameInput(), "Ada Lovelace");
    setNativeInputValue(getEmailInput(), "ada@example.com");
  });
}

function checkConsent(): void {
  act(() => {
    getConsentCheckbox().click();
  });
}

describe("LeadForm", () => {
  it("renders with the consent checkbox unchecked and Submit disabled on mount", () => {
    act(() => {
      root.render(<LeadForm config={baseConfig} />);
    });

    expect(getConsentCheckbox().checked).toBe(false);
    expect(getSubmitButton().disabled).toBe(true);
  });

  it("stays disabled until name+email are filled AND consent is checked; no fetch call happens before Submit", () => {
    act(() => {
      root.render(<LeadForm config={baseConfig} />);
    });

    expect(getSubmitButton().disabled).toBe(true);

    fillRequiredFields();
    // Fields filled, consent still unchecked -> still disabled.
    expect(getSubmitButton().disabled).toBe(true);
    expect(submitLeadMock).not.toHaveBeenCalled();

    checkConsent();
    // Both conditions met -> enabled.
    expect(getSubmitButton().disabled).toBe(false);
    expect(submitLeadMock).not.toHaveBeenCalled();
  });

  it("checking consent alone (blank fields) does not enable Submit", () => {
    act(() => {
      root.render(<LeadForm config={baseConfig} />);
    });

    checkConsent();
    expect(getSubmitButton().disabled).toBe(true);
  });

  it("submitting a valid form calls submitLead once with field values + granted:true consent", async () => {
    let resolveSubmit: (value: LeadResult) => void = () => {};
    submitLeadMock.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveSubmit = resolve;
        }),
    );

    act(() => {
      root.render(<LeadForm config={baseConfig} />);
    });

    fillRequiredFields();
    act(() => {
      // The phone field live-formats as a US number (formatUsPhoneInput) --
      // typed digits, submitted formatted.
      setNativeInputValue(container.querySelector<HTMLInputElement>("#cw-lead-phone")!, "5551234567");
    });
    checkConsent();

    act(() => {
      getSubmitButton().click();
    });

    expect(submitLeadMock).toHaveBeenCalledTimes(1);
    expect(submitLeadMock).toHaveBeenCalledWith(
      baseConfig,
      expect.objectContaining({
        name: "Ada Lovelace",
        email: "ada@example.com",
        phone: "(555) 123-4567",
        consent: { granted: true, purpose: CONSENT_PURPOSE, text: CONSENT_TEXT },
      }),
    );

    // Form disabled while submitting.
    expect(getNameInput().disabled).toBe(true);

    await act(async () => {
      resolveSubmit({ ok: true, lead: { leadId: "lead-1", status: "new" } });
      await Promise.resolve();
    });
  });

  it("the phone field strips letters/symbols as typed and caps at 10 digits", () => {
    act(() => {
      root.render(<LeadForm config={baseConfig} />);
    });

    const phoneInput = container.querySelector<HTMLInputElement>("#cw-lead-phone")!;

    act(() => {
      setNativeInputValue(phoneInput, "call me maybe");
    });
    expect(phoneInput.value).toBe("");

    act(() => {
      setNativeInputValue(phoneInput, "78677567689999");
    });
    expect(phoneInput.value).toBe("(786) 775-6768");
  });

  it("on success, replaces the form with an honest confirmation and removes the resubmit affordance", async () => {
    submitLeadMock.mockResolvedValueOnce({ ok: true, lead: { leadId: "lead-1", status: "new" } });

    act(() => {
      root.render(<LeadForm config={baseConfig} />);
    });

    fillRequiredFields();
    checkConsent();
    act(() => {
      getSubmitButton().click();
    });
    await flush();

    expect(container.querySelector("form")).toBeNull();
    expect(container.querySelector(".cw-lead-submit")).toBeNull();
    expect(container.querySelector(".cw-lead-confirmation")).not.toBeNull();
    expect(container.querySelector(".cw-lead-confirmation")?.textContent).toMatch(/thanks/i);
  });

  it("on failure, shows an honest error line, shows no confirmation text, and re-enables the form", async () => {
    submitLeadMock.mockResolvedValueOnce({
      ok: false,
      error: {
        type: "LEAD_ERROR",
        errorCode: "LLM_ERROR",
        message: "Backend failed.",
        correlationId: "corr-999",
        status: 502,
        retryAfterSeconds: null,
      },
    });
    const consoleErrorSpy = vi.spyOn(console, "error").mockImplementation(() => {});

    act(() => {
      root.render(<LeadForm config={baseConfig} />);
    });

    fillRequiredFields();
    checkConsent();
    act(() => {
      getSubmitButton().click();
    });
    await flush();

    const errorLine = container.querySelector(".cw-lead-error");
    expect(errorLine).not.toBeNull();
    expect(errorLine?.textContent).toMatch(/couldn't save/i);

    expect(container.querySelector(".cw-lead-confirmation")).toBeNull();

    // Form re-enabled for manual retry.
    expect(getNameInput().disabled).toBe(false);
    expect(getSubmitButton().disabled).toBe(false);

    // Exactly one attempt, no retry storm.
    expect(submitLeadMock).toHaveBeenCalledTimes(1);

    // PII-safe logging: error_code/correlation_id present, email/name absent.
    expect(consoleErrorSpy).toHaveBeenCalledTimes(1);
    const loggedArgs = consoleErrorSpy.mock.calls[0] as unknown[];
    const loggedText = loggedArgs.join(" ");
    expect(loggedText).toContain("LLM_ERROR");
    expect(loggedText).toContain("corr-999");
    expect(loggedText).not.toContain("ada@example.com");
    expect(loggedText).not.toContain("Ada Lovelace");
  });

  describe("S14.5 a11y hardening", () => {
    it("every field's label is associated to its input via htmlFor/id", () => {
      act(() => {
        root.render(<LeadForm config={baseConfig} />);
      });

      for (const id of ["cw-lead-name", "cw-lead-email", "cw-lead-phone", "cw-lead-consent"]) {
        const label = container.querySelector<HTMLLabelElement>(`label[for="${id}"]`);
        expect(label, `label for #${id}`).not.toBeNull();
        expect(container.querySelector(`#${id}`)).not.toBeNull();
      }
    });

    it("the consent checkbox is a real, keyboard-toggleable focusable element", () => {
      act(() => {
        root.render(<LeadForm config={baseConfig} />);
      });

      const checkbox = getConsentCheckbox();
      expect(checkbox.tagName).toBe("INPUT");
      expect(checkbox.type).toBe("checkbox");
      expect(checkbox.disabled).toBe(false);

      checkbox.focus();
      expect(document.activeElement).toBe(checkbox);
    });

    it("submit-disabled state is conveyed via the native disabled attribute", () => {
      act(() => {
        root.render(<LeadForm config={baseConfig} />);
      });

      expect(getSubmitButton().hasAttribute("disabled")).toBe(true);

      fillRequiredFields();
      checkConsent();

      expect(getSubmitButton().hasAttribute("disabled")).toBe(false);
    });

    it("focus moves to the success confirmation (role=status) when it appears", async () => {
      submitLeadMock.mockResolvedValueOnce({ ok: true, lead: { leadId: "lead-1", status: "new" } });

      act(() => {
        root.render(<LeadForm config={baseConfig} />);
      });

      fillRequiredFields();
      checkConsent();
      act(() => {
        getSubmitButton().click();
      });
      await flush();

      const confirmation = container.querySelector<HTMLElement>(".cw-lead-confirmation");
      expect(confirmation).not.toBeNull();
      expect(confirmation?.getAttribute("role")).toBe("status");
      expect(document.activeElement).toBe(confirmation);
    });

    it("the error line is role=alert (assertive) on failure", async () => {
      submitLeadMock.mockResolvedValueOnce({
        ok: false,
        error: {
          type: "LEAD_ERROR",
          errorCode: "LLM_ERROR",
          message: "Backend failed.",
          correlationId: "corr-999",
          status: 502,
          retryAfterSeconds: null,
        },
      });
      vi.spyOn(console, "error").mockImplementation(() => {});

      act(() => {
        root.render(<LeadForm config={baseConfig} />);
      });

      fillRequiredFields();
      checkConsent();
      act(() => {
        getSubmitButton().click();
      });
      await flush();

      const errorLine = container.querySelector(".cw-lead-error");
      expect(errorLine?.getAttribute("role")).toBe("alert");
    });
  });
});

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { WidgetConfig } from "../config";
import type { IdentityResult } from "../identity";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const submitIdentityMock = vi.fn<(config: WidgetConfig, input: unknown) => Promise<IdentityResult>>();

vi.mock("../identity", async () => {
  const actual = await vi.importActual<typeof import("../identity")>("../identity");
  return {
    ...actual,
    submitIdentity: (config: WidgetConfig, input: unknown) => submitIdentityMock(config, input),
  };
});

import { IdentityForm } from "./IdentityForm";
import { CHAT_IDENTITY_CONSENT_PURPOSE, CHAT_IDENTITY_CONSENT_TEXT } from "../identity";

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
  submitIdentityMock.mockReset();
});

afterEach(() => {
  act(() => {
    root.unmount();
  });
  container.remove();
  vi.restoreAllMocks();
});

function getNameInput(): HTMLInputElement {
  const input = container.querySelector<HTMLInputElement>("#cw-identity-name");
  if (!input) throw new Error("name input not found");
  return input;
}

function getEmailInput(): HTMLInputElement {
  const input = container.querySelector<HTMLInputElement>("#cw-identity-email");
  if (!input) throw new Error("email input not found");
  return input;
}

function getConsentCheckbox(): HTMLInputElement {
  const input = container.querySelector<HTMLInputElement>("#cw-identity-consent");
  if (!input) throw new Error("consent checkbox not found");
  return input;
}

function getSubmitButton(): HTMLButtonElement {
  const button = container.querySelector<HTMLButtonElement>(".cw-identity-submit");
  if (!button) throw new Error("submit button not found");
  return button;
}

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

describe("IdentityForm", () => {
  it("renders with the consent checkbox unchecked and Submit disabled on mount", () => {
    act(() => {
      root.render(<IdentityForm config={baseConfig} />);
    });

    expect(getConsentCheckbox().checked).toBe(false);
    expect(getSubmitButton().disabled).toBe(true);
  });

  it("stays disabled until name+email are filled AND consent is checked; no fetch call happens before Submit", () => {
    act(() => {
      root.render(<IdentityForm config={baseConfig} />);
    });

    expect(getSubmitButton().disabled).toBe(true);

    fillRequiredFields();
    expect(getSubmitButton().disabled).toBe(true);
    expect(submitIdentityMock).not.toHaveBeenCalled();

    checkConsent();
    expect(getSubmitButton().disabled).toBe(false);
    expect(submitIdentityMock).not.toHaveBeenCalled();
  });

  it("checking consent alone (blank fields) does not enable Submit", () => {
    act(() => {
      root.render(<IdentityForm config={baseConfig} />);
    });

    checkConsent();
    expect(getSubmitButton().disabled).toBe(true);
  });

  it("submitting a valid form calls submitIdentity once with the NEW chat_identification consent purpose/copy", async () => {
    let resolveSubmit: (value: IdentityResult) => void = () => {};
    submitIdentityMock.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveSubmit = resolve;
        }),
    );

    act(() => {
      root.render(<IdentityForm config={baseConfig} />);
    });

    fillRequiredFields();
    checkConsent();

    act(() => {
      getSubmitButton().click();
    });

    expect(submitIdentityMock).toHaveBeenCalledTimes(1);
    expect(submitIdentityMock).toHaveBeenCalledWith(
      baseConfig,
      expect.objectContaining({
        name: "Ada Lovelace",
        email: "ada@example.com",
        consent: { granted: true, purpose: CHAT_IDENTITY_CONSENT_PURPOSE, text: CHAT_IDENTITY_CONSENT_TEXT },
      }),
    );

    // Form disabled while submitting.
    expect(getNameInput().disabled).toBe(true);

    await act(async () => {
      resolveSubmit({ ok: true, identity: { leadId: "lead-1", status: "new" } });
      await Promise.resolve();
    });
  });

  it("on success, calls onCaptured exactly once and replaces the form with an honest confirmation", async () => {
    submitIdentityMock.mockResolvedValueOnce({ ok: true, identity: { leadId: "lead-1", status: "new" } });
    const onCaptured = vi.fn();

    act(() => {
      root.render(<IdentityForm config={baseConfig} onCaptured={onCaptured} />);
    });

    fillRequiredFields();
    checkConsent();
    act(() => {
      getSubmitButton().click();
    });
    await flush();

    expect(container.querySelector("form")).toBeNull();
    expect(container.querySelector(".cw-identity-submit")).toBeNull();
    expect(container.querySelector(".cw-identity-confirmation")).not.toBeNull();
    expect(onCaptured).toHaveBeenCalledTimes(1);
  });

  it("on failure, shows an honest error line, does NOT call onCaptured, and re-enables the form", async () => {
    submitIdentityMock.mockResolvedValueOnce({
      ok: false,
      error: {
        type: "IDENTITY_ERROR",
        errorCode: "LLM_ERROR",
        message: "Backend failed.",
        correlationId: "corr-999",
        status: 502,
        retryAfterSeconds: null,
      },
    });
    const onCaptured = vi.fn();
    const consoleErrorSpy = vi.spyOn(console, "error").mockImplementation(() => {});

    act(() => {
      root.render(<IdentityForm config={baseConfig} onCaptured={onCaptured} />);
    });

    fillRequiredFields();
    checkConsent();
    act(() => {
      getSubmitButton().click();
    });
    await flush();

    const errorLine = container.querySelector(".cw-identity-error");
    expect(errorLine).not.toBeNull();
    expect(errorLine?.textContent).toMatch(/couldn't save/i);
    expect(container.querySelector(".cw-identity-confirmation")).toBeNull();
    expect(onCaptured).not.toHaveBeenCalled();

    // Form re-enabled for manual retry -- no fabricated success, nothing re-sent.
    expect(getNameInput().disabled).toBe(false);
    expect(getSubmitButton().disabled).toBe(false);
    expect(submitIdentityMock).toHaveBeenCalledTimes(1);

    // PII-safe logging: error_code/correlation_id present, email/name absent.
    expect(consoleErrorSpy).toHaveBeenCalledTimes(1);
    const loggedArgs = consoleErrorSpy.mock.calls[0] as unknown[];
    const loggedText = loggedArgs.join(" ");
    expect(loggedText).toContain("LLM_ERROR");
    expect(loggedText).toContain("corr-999");
    expect(loggedText).not.toContain("ada@example.com");
    expect(loggedText).not.toContain("Ada Lovelace");
  });

  describe("a11y", () => {
    it("every field's label is associated to its input via htmlFor/id", () => {
      act(() => {
        root.render(<IdentityForm config={baseConfig} />);
      });

      for (const id of ["cw-identity-name", "cw-identity-email", "cw-identity-consent"]) {
        const label = container.querySelector<HTMLLabelElement>(`label[for="${id}"]`);
        expect(label, `label for #${id}`).not.toBeNull();
        expect(container.querySelector(`#${id}`)).not.toBeNull();
      }
    });

    it("focus moves to the first field on mount", () => {
      act(() => {
        root.render(<IdentityForm config={baseConfig} />);
      });

      expect(document.activeElement).toBe(getNameInput());
    });

    it("focus moves to the success confirmation (role=status) when it appears", async () => {
      submitIdentityMock.mockResolvedValueOnce({ ok: true, identity: { leadId: "lead-1", status: "new" } });

      act(() => {
        root.render(<IdentityForm config={baseConfig} />);
      });

      fillRequiredFields();
      checkConsent();
      act(() => {
        getSubmitButton().click();
      });
      await flush();

      const confirmation = container.querySelector<HTMLElement>(".cw-identity-confirmation");
      expect(confirmation).not.toBeNull();
      expect(confirmation?.getAttribute("role")).toBe("status");
      expect(document.activeElement).toBe(confirmation);
    });

    it("the error line is role=alert (assertive) on failure", async () => {
      submitIdentityMock.mockResolvedValueOnce({
        ok: false,
        error: {
          type: "IDENTITY_ERROR",
          errorCode: "LLM_ERROR",
          message: "Backend failed.",
          correlationId: "corr-999",
          status: 502,
          retryAfterSeconds: null,
        },
      });
      vi.spyOn(console, "error").mockImplementation(() => {});

      act(() => {
        root.render(<IdentityForm config={baseConfig} />);
      });

      fillRequiredFields();
      checkConsent();
      act(() => {
        getSubmitButton().click();
      });
      await flush();

      const errorLine = container.querySelector(".cw-identity-error");
      expect(errorLine?.getAttribute("role")).toBe("alert");
    });
  });
});

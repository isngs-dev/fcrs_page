import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { WidgetConfig } from "../config";
import type { LeadResult } from "../lead";
import type { IdentityResult } from "../identity";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const submitLeadMock = vi.fn<(config: WidgetConfig, input: unknown) => Promise<LeadResult>>();
const submitIdentityMock = vi.fn<(config: WidgetConfig, input: unknown) => Promise<IdentityResult>>();
let offer: { email: boolean; call: boolean; version: string } | null = null;

vi.mock("../lead", async () => {
  const actual = await vi.importActual<typeof import("../lead")>("../lead");
  return { ...actual, submitLead: (c: WidgetConfig, i: unknown) => submitLeadMock(c, i) };
});
vi.mock("../identity", async () => {
  const actual = await vi.importActual<typeof import("../identity")>("../identity");
  return { ...actual, submitIdentity: (c: WidgetConfig, i: unknown) => submitIdentityMock(c, i) };
});
vi.mock("../session", async () => {
  const actual = await vi.importActual<typeof import("../session")>("../session");
  return { ...actual, getOutreachConsent: () => offer };
});

import { CALL_CONSENT_TEXT, CHANNEL_CONSENT_VERSION, EMAIL_CONSENT_TEXT } from "../channelConsent";
import { IdentityForm } from "./IdentityForm";
import { LeadForm } from "./LeadForm";

const config: WidgetConfig = {
  clientKey: "pk_test_123",
  apiBase: "http://localhost:8000",
  mountSelector: null,
  debug: false,
  position: "right",
};

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  submitLeadMock.mockReset().mockResolvedValue({ ok: true, lead: { leadId: "l1", status: "new" } });
  submitIdentityMock.mockReset().mockResolvedValue({ ok: true, identity: { leadId: "l1", status: "new" } } as never);
  offer = null;
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

function $(selector: string): HTMLInputElement {
  const el = container.querySelector<HTMLInputElement>(selector);
  if (!el) throw new Error(`${selector} not found`);
  return el;
}

function type(selector: string, text: string): void {
  // eslint-disable-next-line @typescript-eslint/unbound-method
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value")!.set!;
  act(() => {
    Reflect.apply(setter, $(selector), [text]);
    $(selector).dispatchEvent(new Event("input", { bubbles: true }));
  });
}

function click(selector: string): void {
  act(() => $(selector).click());
}

async function submit(formSelector: string): Promise<void> {
  await act(async () => {
    container.querySelector<HTMLFormElement>(formSelector)!.requestSubmit();
    await Promise.resolve();
    await Promise.resolve();
  });
}

describe("LeadForm follow-up consent (A4)", () => {
  it("shows no boxes and sends no channel_consents when the tenant offers none", async () => {
    act(() => root.render(<LeadForm config={config} />));
    expect(container.querySelector(".cw-channel-consents")).toBeNull();
    type("#cw-lead-name", "Ada");
    type("#cw-lead-email", "ada@example.com");
    click("#cw-lead-consent");
    await submit(".cw-lead-form");
    expect(submitLeadMock.mock.calls[0]![1]).not.toHaveProperty("channelConsents");
  });

  it("hides the boxes when the server's wording version differs from this bundle's", () => {
    offer = { email: true, call: true, version: "1999-01-z" };
    act(() => root.render(<LeadForm config={config} />));
    expect(container.querySelector(".cw-channel-consents")).toBeNull();
  });

  it("offers email + call, unticked; call needs a phone; payload carries the version", async () => {
    offer = { email: true, call: true, version: CHANNEL_CONSENT_VERSION };
    act(() => root.render(<LeadForm config={config} />));
    expect($("#cw-lead-consent-email").checked).toBe(false);
    expect($("#cw-lead-consent-call").checked).toBe(false);
    expect($("#cw-lead-consent-call").disabled).toBe(true);
    expect(container.textContent).toContain(EMAIL_CONSENT_TEXT);
    expect(container.textContent).toContain(CALL_CONSENT_TEXT);

    type("#cw-lead-name", "Ada");
    type("#cw-lead-email", "ada@example.com");
    type("#cw-lead-phone", "7868233553");
    expect($("#cw-lead-consent-call").disabled).toBe(false);
    click("#cw-lead-consent");
    click("#cw-lead-consent-email");
    click("#cw-lead-consent-call");
    await submit(".cw-lead-form");

    const input = submitLeadMock.mock.calls[0]![1] as Record<string, unknown>;
    expect(input.channelConsents).toEqual({ email: true, call: true, version: CHANNEL_CONSENT_VERSION });
    expect(typeof input.timezone).toBe("string");
  });

  it("the storage consent alone still submits with both channel boxes false", async () => {
    offer = { email: true, call: false, version: CHANNEL_CONSENT_VERSION };
    act(() => root.render(<LeadForm config={config} />));
    expect(container.querySelector("#cw-lead-consent-call")).toBeNull();
    type("#cw-lead-name", "Ada");
    type("#cw-lead-email", "ada@example.com");
    click("#cw-lead-consent");
    await submit(".cw-lead-form");
    const input = submitLeadMock.mock.calls[0]![1] as Record<string, unknown>;
    expect(input.channelConsents).toEqual({ email: false, call: false, version: CHANNEL_CONSENT_VERSION });
  });
});

describe("IdentityForm follow-up consent (A4)", () => {
  it("offers only the email box (no phone in this form)", async () => {
    offer = { email: true, call: true, version: CHANNEL_CONSENT_VERSION };
    act(() => root.render(<IdentityForm config={config} />));
    expect(container.querySelector("#cw-identity-consent-call")).toBeNull();
    type("#cw-identity-name", "Ada");
    type("#cw-identity-email", "ada@example.com");
    click("#cw-identity-consent");
    click("#cw-identity-consent-email");
    await submit(".cw-identity-form");
    const input = submitIdentityMock.mock.calls[0]![1] as Record<string, unknown>;
    expect(input.channelConsents).toEqual({ email: true, call: false, version: CHANNEL_CONSENT_VERSION });
  });
});

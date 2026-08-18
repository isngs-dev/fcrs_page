import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { WidgetConfig } from "../config";
import type { AvailabilitySummary, BookSlotResult, FetchSlotsResult } from "../schedule";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const fetchSlotsMock = vi.fn<(config: WidgetConfig, input: unknown) => Promise<FetchSlotsResult>>();
const bookSlotMock = vi.fn<(config: WidgetConfig, input: unknown) => Promise<BookSlotResult>>();

vi.mock("../schedule", async () => {
  const actual = await vi.importActual<typeof import("../schedule")>("../schedule");
  return {
    ...actual,
    fetchSlots: (config: WidgetConfig, input: unknown) => fetchSlotsMock(config, input),
    bookSlot: (config: WidgetConfig, input: unknown) => bookSlotMock(config, input),
  };
});

import { ScheduleCta } from "./ScheduleCta";
import { SCHEDULE_CONSENT_TEXT } from "../schedule";

const baseConfig: WidgetConfig = {
  clientKey: "pk_test_123",
  apiBase: "http://localhost:8000",
  mountSelector: null,
  debug: false,
};

const SLOT_A = { startsAt: "2026-07-20T09:00:00+00:00", endsAt: "2026-07-20T09:30:00+00:00" };
const SLOT_B = { startsAt: "2026-07-20T10:00:00+00:00", endsAt: "2026-07-20T10:30:00+00:00" };

let container: HTMLDivElement;
let root: Root;

function flush(): Promise<void> {
  return act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
}

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  fetchSlotsMock.mockReset();
  bookSlotMock.mockReset();
});

afterEach(() => {
  act(() => {
    root.unmount();
  });
  container.remove();
  vi.restoreAllMocks();
});

function getSlotButtons(): HTMLButtonElement[] {
  return Array.from(container.querySelectorAll<HTMLButtonElement>(".cw-sched-slot"));
}

function getSlotButton(index: number): HTMLButtonElement {
  const button = getSlotButtons()[index];
  if (!button) throw new Error(`slot button at index ${index} not found`);
  return button;
}

function getConsentCheckbox(): HTMLInputElement {
  const input = container.querySelector<HTMLInputElement>("#cw-sched-consent");
  if (!input) throw new Error("consent checkbox not found");
  return input;
}

function getConfirmButton(): HTMLButtonElement {
  const button = container.querySelector<HTMLButtonElement>(".cw-sched-confirm-button");
  if (!button) throw new Error("confirm button not found");
  return button;
}

/** React tracks <input> values via the native property descriptor — a plain
 * `.value =` write is invisible to React's change detection in jsdom. See
 * ChatWidget.test.tsx's twin helper for the same trick. */
function setNativeInputValue(input: HTMLInputElement, text: string): void {
  // eslint-disable-next-line @typescript-eslint/unbound-method
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value")!.set!;
  Reflect.apply(setter, input, [text]);
  input.dispatchEvent(new Event("input", { bubbles: true }));
}

describe("ScheduleCta", () => {
  it("calls fetchSlots once on mount and renders the returned slots with local-timezone labels", async () => {
    fetchSlotsMock.mockResolvedValueOnce({ ok: true, slots: [SLOT_A, SLOT_B] });

    act(() => {
      root.render(<ScheduleCta config={baseConfig} />);
    });
    await flush();

    expect(fetchSlotsMock).toHaveBeenCalledTimes(1);
    const buttons = getSlotButtons();
    expect(buttons).toHaveLength(2);
    // The displayed label must not be the raw UTC string verbatim (it's localized).
    const firstButton = getSlotButton(0);
    expect(firstButton.textContent).not.toBe(SLOT_A.startsAt);
    expect(firstButton.textContent?.length).toBeGreaterThan(0);
  });

  it("renders an honest 'no times available' message on an empty slot list, never a fabricated slot", async () => {
    fetchSlotsMock.mockResolvedValueOnce({ ok: true, slots: [] });

    act(() => {
      root.render(<ScheduleCta config={baseConfig} />);
    });
    await flush();

    expect(getSlotButtons()).toHaveLength(0);
    const empty = container.querySelector(".cw-sched-empty");
    expect(empty).not.toBeNull();
    expect(empty?.textContent).toMatch(/no times/i);
  });

  it("renders an honest error on a fetchSlots failure, no faked slots", async () => {
    fetchSlotsMock.mockResolvedValueOnce({
      ok: false,
      error: {
        type: "SCHEDULE_ERROR",
        errorCode: "NETWORK_ERROR",
        message: "Network request failed.",
        correlationId: null,
        status: null,
        retryAfterSeconds: null,
      },
    });
    const consoleErrorSpy = vi.spyOn(console, "error").mockImplementation(() => {});

    act(() => {
      root.render(<ScheduleCta config={baseConfig} />);
    });
    await flush();

    expect(getSlotButtons()).toHaveLength(0);
    const errorLine = container.querySelector(".cw-sched-error");
    expect(errorLine).not.toBeNull();
    expect(consoleErrorSpy).toHaveBeenCalled();
  });

  it("selecting a slot shows the confirm step with an unchecked consent checkbox and a disabled Confirm; enables on check", async () => {
    fetchSlotsMock.mockResolvedValueOnce({ ok: true, slots: [SLOT_A] });

    act(() => {
      root.render(<ScheduleCta config={baseConfig} />);
    });
    await flush();

    act(() => {
      getSlotButton(0).click();
    });

    const checkbox = getConsentCheckbox();
    expect(checkbox.checked).toBe(false);
    expect(getConfirmButton().disabled).toBe(true);
    expect(bookSlotMock).not.toHaveBeenCalled();

    act(() => {
      checkbox.click();
    });
    expect(getConfirmButton().disabled).toBe(false);
    expect(bookSlotMock).not.toHaveBeenCalled();
  });

  it("consent label text matches the exported SCHEDULE_CONSENT_TEXT (shown == sent)", async () => {
    fetchSlotsMock.mockResolvedValueOnce({ ok: true, slots: [SLOT_A] });

    act(() => {
      root.render(<ScheduleCta config={baseConfig} />);
    });
    await flush();
    act(() => {
      getSlotButton(0).click();
    });

    const label = container.querySelector(".cw-sched-consent-label");
    expect(label?.textContent).toBe(SCHEDULE_CONSENT_TEXT);
  });

  it("confirming calls bookSlot once with the exact selected UTC starts_at and granted:true consent; shows confirmation on 201", async () => {
    fetchSlotsMock.mockResolvedValueOnce({ ok: true, slots: [SLOT_A] });
    bookSlotMock.mockResolvedValueOnce({
      ok: true,
      booking: { eventId: "evt-1", startsAt: SLOT_A.startsAt, endsAt: SLOT_A.endsAt, status: "booked" },
    });

    act(() => {
      root.render(<ScheduleCta config={baseConfig} />);
    });
    await flush();
    act(() => {
      getSlotButton(0).click();
    });
    act(() => {
      getConsentCheckbox().click();
    });
    act(() => {
      getConfirmButton().click();
    });
    await flush();

    expect(bookSlotMock).toHaveBeenCalledTimes(1);
    const [, input] = bookSlotMock.mock.calls[0] as [WidgetConfig, { startsAt: string; consent: { granted: boolean } }];
    expect(input.startsAt).toBe(SLOT_A.startsAt);
    expect(input.consent.granted).toBe(true);

    // Confirmation renders, picker is gone (no rebook affordance).
    const confirmation = container.querySelector(".cw-sched-confirmation");
    expect(confirmation).not.toBeNull();
    expect(getSlotButtons()).toHaveLength(0);
    expect(container.querySelector(".cw-sched-confirm-button")).toBeNull();
  });

  it("on SLOT_UNAVAILABLE, shows an honest error and re-fetches slots (second fetchSlots call), no confirmation", async () => {
    fetchSlotsMock.mockResolvedValueOnce({ ok: true, slots: [SLOT_A] });
    fetchSlotsMock.mockResolvedValueOnce({ ok: true, slots: [SLOT_B] });
    bookSlotMock.mockResolvedValueOnce({
      ok: false,
      error: {
        type: "SCHEDULE_ERROR",
        errorCode: "SLOT_UNAVAILABLE",
        message: "The requested time is no longer available.",
        correlationId: "corr-1",
        status: 422,
        retryAfterSeconds: null,
      },
    });
    const consoleErrorSpy = vi.spyOn(console, "error").mockImplementation(() => {});

    act(() => {
      root.render(<ScheduleCta config={baseConfig} />);
    });
    await flush();
    act(() => {
      getSlotButton(0).click();
    });
    act(() => {
      getConsentCheckbox().click();
    });
    act(() => {
      getConfirmButton().click();
    });
    await flush();

    expect(bookSlotMock).toHaveBeenCalledTimes(1);
    expect(fetchSlotsMock).toHaveBeenCalledTimes(2);
    expect(container.querySelector(".cw-sched-confirmation")).toBeNull();
    // Re-fetched slot list is shown (the taken slot is gone, replaced by SLOT_B).
    expect(getSlotButtons()).toHaveLength(1);
    expect(consoleErrorSpy).toHaveBeenCalled();
  });

  it("on CALENDAR_SYNC_FAILED, shows an honest error with no confirmation and no auto-retry", async () => {
    fetchSlotsMock.mockResolvedValueOnce({ ok: true, slots: [SLOT_A] });
    bookSlotMock.mockResolvedValueOnce({
      ok: false,
      error: {
        type: "SCHEDULE_ERROR",
        errorCode: "CALENDAR_SYNC_FAILED",
        message: "Failed to sync the booking to the calendar. Please try again.",
        correlationId: "corr-2",
        status: 422,
        retryAfterSeconds: null,
      },
    });
    const consoleErrorSpy = vi.spyOn(console, "error").mockImplementation(() => {});

    act(() => {
      root.render(<ScheduleCta config={baseConfig} />);
    });
    await flush();
    act(() => {
      getSlotButton(0).click();
    });
    act(() => {
      getConsentCheckbox().click();
    });
    act(() => {
      getConfirmButton().click();
    });
    await flush();

    expect(bookSlotMock).toHaveBeenCalledTimes(1);
    // No re-fetch for a non-SLOT_UNAVAILABLE failure (no auto-retry/loop).
    expect(fetchSlotsMock).toHaveBeenCalledTimes(1);
    expect(container.querySelector(".cw-sched-confirmation")).toBeNull();
    const errorLine = container.querySelector(".cw-sched-error");
    expect(errorLine).not.toBeNull();
    expect(consoleErrorSpy).toHaveBeenCalled();
  });

  it("never logs booked-time/PII beyond error_code/correlation_id on failure", async () => {
    fetchSlotsMock.mockResolvedValueOnce({ ok: true, slots: [SLOT_A] });
    bookSlotMock.mockResolvedValueOnce({
      ok: false,
      error: {
        type: "SCHEDULE_ERROR",
        errorCode: "CALENDAR_SYNC_FAILED",
        message: "Failed to sync the booking to the calendar. Please try again.",
        correlationId: "corr-2",
        status: 422,
        retryAfterSeconds: null,
      },
    });
    const consoleErrorSpy = vi.spyOn(console, "error").mockImplementation(() => {});

    act(() => {
      root.render(<ScheduleCta config={baseConfig} />);
    });
    await flush();
    act(() => {
      getSlotButton(0).click();
    });
    act(() => {
      getConsentCheckbox().click();
    });
    act(() => {
      getConfirmButton().click();
    });
    await flush();

    const loggedText = consoleErrorSpy.mock.calls.map((c) => c.join(" ")).join(" ");
    expect(loggedText).toContain("CALENDAR_SYNC_FAILED");
    expect(loggedText).toContain("corr-2");
    expect(loggedText).not.toContain(SLOT_A.startsAt);
  });

  describe("S14.5 a11y hardening", () => {
    it("slot buttons are real focusable <button>s whose accessible name conveys the time", async () => {
      fetchSlotsMock.mockResolvedValueOnce({ ok: true, slots: [SLOT_A, SLOT_B] });

      act(() => {
        root.render(<ScheduleCta config={baseConfig} />);
      });
      await flush();

      const buttons = getSlotButtons();
      expect(buttons).toHaveLength(2);
      for (const button of buttons) {
        expect(button.tagName).toBe("BUTTON");
        expect(button.type).toBe("button");
        expect(button.disabled).toBe(false);
        // Accessible name == visible text content, which formatLocalSlotLabel
        // renders as a localized time string (not the raw UTC string).
        expect(button.textContent?.length).toBeGreaterThan(0);
      }
    });

    it("the slot list is labelled via aria-labelledby", async () => {
      fetchSlotsMock.mockResolvedValueOnce({ ok: true, slots: [SLOT_A] });

      act(() => {
        root.render(<ScheduleCta config={baseConfig} />);
      });
      await flush();

      const list = container.querySelector("ul.cw-sched-list");
      const labelledBy = list?.getAttribute("aria-labelledby");
      expect(labelledBy).toBeTruthy();
      expect(container.querySelector(`#${labelledBy}`)?.textContent).toMatch(/choose a time/i);
    });

    it("the empty-state and loading state are announced via role=status", async () => {
      fetchSlotsMock.mockResolvedValueOnce({ ok: true, slots: [] });

      act(() => {
        root.render(<ScheduleCta config={baseConfig} />);
      });

      // Immediately after mount (before the mocked fetch resolves), the
      // loading state should be announced.
      expect(container.querySelector('[role="status"]')).not.toBeNull();

      await flush();

      const empty = container.querySelector(".cw-sched-empty");
      expect(empty?.getAttribute("role")).toBe("status");
    });

    it("the consent checkbox is keyboard-toggleable and labelled", async () => {
      fetchSlotsMock.mockResolvedValueOnce({ ok: true, slots: [SLOT_A] });

      act(() => {
        root.render(<ScheduleCta config={baseConfig} />);
      });
      await flush();
      act(() => {
        getSlotButton(0).click();
      });

      const checkbox = getConsentCheckbox();
      expect(checkbox.tagName).toBe("INPUT");
      expect(checkbox.type).toBe("checkbox");
      const label = container.querySelector(`label[for="${checkbox.id}"]`);
      expect(label).not.toBeNull();

      checkbox.focus();
      expect(document.activeElement).toBe(checkbox);
    });

    it("focus moves to the confirm heading on the list->confirm transition", async () => {
      fetchSlotsMock.mockResolvedValueOnce({ ok: true, slots: [SLOT_A] });

      act(() => {
        root.render(<ScheduleCta config={baseConfig} />);
      });
      await flush();
      act(() => {
        getSlotButton(0).click();
      });

      const heading = container.querySelector<HTMLElement>(".cw-sched-confirm-heading");
      expect(heading).not.toBeNull();
      expect(document.activeElement).toBe(heading);
    });

    it("focus moves to the booking confirmation on the confirm->booked transition", async () => {
      fetchSlotsMock.mockResolvedValueOnce({ ok: true, slots: [SLOT_A] });
      bookSlotMock.mockResolvedValueOnce({
        ok: true,
        booking: { eventId: "evt-1", startsAt: SLOT_A.startsAt, endsAt: SLOT_A.endsAt, status: "booked" },
      });

      act(() => {
        root.render(<ScheduleCta config={baseConfig} />);
      });
      await flush();
      act(() => {
        getSlotButton(0).click();
      });
      act(() => {
        getConsentCheckbox().click();
      });
      act(() => {
        getConfirmButton().click();
      });
      await flush();

      const confirmation = container.querySelector<HTMLElement>(".cw-sched-confirmation");
      expect(confirmation).not.toBeNull();
      expect(confirmation?.getAttribute("role")).toBe("status");
      expect(document.activeElement).toBe(confirmation);
    });

    it("the booking error line is role=alert (assertive)", async () => {
      fetchSlotsMock.mockResolvedValueOnce({ ok: true, slots: [SLOT_A] });
      bookSlotMock.mockResolvedValueOnce({
        ok: false,
        error: {
          type: "SCHEDULE_ERROR",
          errorCode: "CALENDAR_SYNC_FAILED",
          message: "Failed to sync the booking to the calendar. Please try again.",
          correlationId: "corr-2",
          status: 422,
          retryAfterSeconds: null,
        },
      });
      vi.spyOn(console, "error").mockImplementation(() => {});

      act(() => {
        root.render(<ScheduleCta config={baseConfig} />);
      });
      await flush();
      act(() => {
        getSlotButton(0).click();
      });
      act(() => {
        getConsentCheckbox().click();
      });
      act(() => {
        getConfirmButton().click();
      });
      await flush();

      const errorLine = container.querySelector(".cw-sched-error");
      expect(errorLine?.getAttribute("role")).toBe("alert");
    });
  });

  describe("SR-5: staged flow (summary supplied — calendar/timezone/email/recap)", () => {
    const SUMMARY: AvailabilitySummary = {
      action: "schedule_cta",
      timezone: "America/New_York",
      days: [
        { date: "2026-07-20", hasAvailability: false },
        { date: "2026-07-21", hasAvailability: true },
        { date: "2026-07-22", hasAvailability: true },
      ],
      transitionMessage: "I'd be happy to help you find a time with our sales team.",
      existingBooking: null,
    };

    function getCalendarDayButtons(): HTMLButtonElement[] {
      return Array.from(container.querySelectorAll<HTMLButtonElement>(".cw-sched-day"));
    }

    function getEnabledCalendarDayButton(): HTMLButtonElement {
      const button = getCalendarDayButtons().find((b) => !b.disabled);
      if (!button) throw new Error("no enabled calendar day button found");
      return button;
    }

    function getTimezoneSelect(): HTMLSelectElement {
      const select = container.querySelector<HTMLSelectElement>("#cw-sched-timezone");
      if (!select) throw new Error("timezone select not found");
      return select;
    }

    function getEmailInput(): HTMLInputElement {
      const input = container.querySelector<HTMLInputElement>("#cw-sched-email");
      if (!input) throw new Error("email input not found");
      return input;
    }

    function continueFromTimeSelection(): void {
      const button = container.querySelector<HTMLButtonElement>(".cw-sched-continue-button");
      if (!button) throw new Error("continue button not found");
      act(() => {
        button.click();
      });
    }

    it("renders a real month calendar (role=grid > role=row > role=gridcell) with only server-marked days enabled", async () => {
      act(() => {
        root.render(<ScheduleCta config={baseConfig} summary={SUMMARY} />);
      });
      await flush();

      expect(fetchSlotsMock).not.toHaveBeenCalled();
      const grid = container.querySelector('[role="grid"]');
      expect(grid).not.toBeNull();
      expect(container.querySelectorAll('[role="row"]').length).toBeGreaterThan(0);
      expect(container.querySelectorAll('[role="gridcell"]').length).toBeGreaterThan(0);

      const dayButtons = getCalendarDayButtons();
      expect(dayButtons.length).toBe(3);
      const disabledDay = dayButtons.find((b) => b.querySelector(".cw-sched-day-number")?.textContent === "20");
      const enabledDay = dayButtons.find((b) => b.querySelector(".cw-sched-day-number")?.textContent === "21");
      expect(disabledDay?.disabled).toBe(true);
      expect(enabledDay?.disabled).toBe(false);
    });

    it("renders a timezone selector defaulting to the server's tenant timezone", async () => {
      fetchSlotsMock.mockResolvedValueOnce({ ok: true, slots: [SLOT_A] });
      act(() => {
        root.render(<ScheduleCta config={baseConfig} summary={SUMMARY} />);
      });
      await flush();
      act(() => {
        getEnabledCalendarDayButton().click();
      });
      await flush();

      const select = getTimezoneSelect();
      expect(select.value).toBe("America/New_York");
      const options = Array.from(select.querySelectorAll("option")).map((o) => o.value);
      expect(options).toContain("America/New_York");
      expect(options.length).toBeGreaterThan(1);
    });

    it("renders the meeting-card header and dismisses the scheduling card from its close control", async () => {
      act(() => {
        root.render(<ScheduleCta config={baseConfig} summary={SUMMARY} />);
      });
      await flush();

      expect(container.querySelector(".cw-sched-card-header")?.textContent).toMatch(/Schedule a meeting/i);
      expect(container.querySelector(".cw-sched-rep")?.textContent).toMatch(/Sales team/i);
      const close = container.querySelector<HTMLButtonElement>('.cw-sched-close[aria-label="Close scheduling"]');
      act(() => {
        close?.click();
      });
      expect(container.querySelector(".cw-sched-card")).toBeNull();
    });

    it("keeps the invite step behind an explicit time selection and Continue action", async () => {
      fetchSlotsMock.mockResolvedValueOnce({ ok: true, slots: [SLOT_A, SLOT_B] });
      act(() => {
        root.render(<ScheduleCta config={baseConfig} summary={SUMMARY} />);
      });
      await flush();
      act(() => {
        getEnabledCalendarDayButton().click();
      });
      await flush();

      const continueButton = container.querySelector<HTMLButtonElement>(".cw-sched-continue-button");
      expect(continueButton?.disabled).toBe(true);
      expect(container.querySelector("#cw-sched-email")).toBeNull();

      act(() => {
        getSlotButton(0).click();
      });
      expect(continueButton?.disabled).toBe(false);
      continueFromTimeSelection();
      expect(container.querySelector("#cw-sched-email")).not.toBeNull();
    });

    it("picking an enabled day fetches and renders the 3-column time grid for that day, and the booking carries the chosen timezone", async () => {
      fetchSlotsMock.mockResolvedValueOnce({ ok: true, slots: [SLOT_A] });
      bookSlotMock.mockResolvedValueOnce({
        ok: true,
        booking: { eventId: "evt-1", startsAt: SLOT_A.startsAt, endsAt: SLOT_A.endsAt, status: "booked" },
      });

      act(() => {
        root.render(<ScheduleCta config={baseConfig} summary={SUMMARY} />);
      });
      await flush();

      act(() => {
        getEnabledCalendarDayButton().click();
      });
      await flush();

      expect(fetchSlotsMock).toHaveBeenCalledTimes(1);
      const [, input] = fetchSlotsMock.mock.calls[0] as [WidgetConfig, { dateFrom?: string; dateTo?: string }];
      expect(input.dateFrom).toBe("2026-07-21");
      expect(input.dateTo).toBe("2026-07-21");
      expect(getSlotButtons()).toHaveLength(1);

      // Select a time, then continue to the invite step. The booking must
      // carry the timezone chosen in the preceding time-selection step.
      act(() => {
        getSlotButton(0).click();
      });
      continueFromTimeSelection();
      const select = container.querySelector<HTMLSelectElement>("#cw-sched-timezone");
      // The timezone selector only exists on the time step; the invite step
      // carries the value via component state.
      expect(select).toBeNull();

      act(() => {
        setNativeInputValue(getEmailInput(), "invite@example.com");
      });
      act(() => {
        getConsentCheckbox().click();
      });
      act(() => {
        getConfirmButton().click();
      });
      await flush();

      expect(bookSlotMock).toHaveBeenCalledTimes(1);
      const [, bookInput] = bookSlotMock.mock.calls[0] as [WidgetConfig, { timezone: string; email?: string }];
      expect(bookInput.timezone).toBe("America/New_York");
      expect(bookInput.email).toBe("invite@example.com");
      expect(container.querySelector(".cw-sched-booked-message")?.textContent).toContain("invite@example.com");
      expect(container.querySelector(".cw-sched-success-card")?.textContent).toMatch(/Sales team/i);
    });

    it("phone: strips non-digit characters live-typing and formats/sends a US number", async () => {
      fetchSlotsMock.mockResolvedValueOnce({ ok: true, slots: [SLOT_A] });
      bookSlotMock.mockResolvedValueOnce({
        ok: true,
        booking: { eventId: "evt-1", startsAt: SLOT_A.startsAt, endsAt: SLOT_A.endsAt, status: "booked" },
      });

      act(() => {
        root.render(<ScheduleCta config={baseConfig} summary={SUMMARY} />);
      });
      await flush();
      act(() => {
        getEnabledCalendarDayButton().click();
      });
      await flush();
      act(() => {
        getSlotButton(0).click();
      });
      continueFromTimeSelection();

      expect(container.querySelector(".cw-sched-phone-code")).toBeNull();
      const phoneInput = container.querySelector<HTMLInputElement>(".cw-sched-phone-input");
      if (!phoneInput) throw new Error("phone input not found");

      // Letters/symbols are stripped as typed -- not just rejected on submit
      // -- and the digits are progressively formatted as a US number.
      act(() => {
        setNativeInputValue(phoneInput, "(786) abc775-6768xyz9999");
      });
      expect(phoneInput.value).toBe("(786) 775-6768");

      act(() => {
        setNativeInputValue(getEmailInput(), "invite@example.com");
      });
      act(() => {
        getConsentCheckbox().click();
      });
      act(() => {
        getConfirmButton().click();
      });
      await flush();

      expect(bookSlotMock).toHaveBeenCalledTimes(1);
      const [, bookInput] = bookSlotMock.mock.calls[0] as [WidgetConfig, { phone?: string }];
      expect(bookInput.phone).toBe("(786) 775-6768");
    });

    it("omits phone entirely when the number field is left blank", async () => {
      fetchSlotsMock.mockResolvedValueOnce({ ok: true, slots: [SLOT_A] });
      bookSlotMock.mockResolvedValueOnce({
        ok: true,
        booking: { eventId: "evt-1", startsAt: SLOT_A.startsAt, endsAt: SLOT_A.endsAt, status: "booked" },
      });

      act(() => {
        root.render(<ScheduleCta config={baseConfig} summary={SUMMARY} />);
      });
      await flush();
      act(() => {
        getEnabledCalendarDayButton().click();
      });
      await flush();
      act(() => {
        getSlotButton(0).click();
      });
      continueFromTimeSelection();

      act(() => {
        setNativeInputValue(getEmailInput(), "invite@example.com");
      });
      act(() => {
        getConsentCheckbox().click();
      });
      act(() => {
        getConfirmButton().click();
      });
      await flush();

      expect(bookSlotMock).toHaveBeenCalledTimes(1);
      const [, bookInput] = bookSlotMock.mock.calls[0] as [WidgetConfig, { phone?: string }];
      expect(bookInput.phone).toBeUndefined();
    });

    it("the email step gates Confirm behind both consent AND a non-empty email", async () => {
      fetchSlotsMock.mockResolvedValueOnce({ ok: true, slots: [SLOT_A] });

      act(() => {
        root.render(<ScheduleCta config={baseConfig} summary={SUMMARY} />);
      });
      await flush();
      act(() => {
        getEnabledCalendarDayButton().click();
      });
      await flush();
      act(() => {
        getSlotButton(0).click();
      });
      continueFromTimeSelection();

      // Consent alone, no email -> still disabled.
      act(() => {
        getConsentCheckbox().click();
      });
      expect(getConfirmButton().disabled).toBe(true);

      act(() => {
        setNativeInputValue(getEmailInput(), "invite@example.com");
      });
      expect(getConfirmButton().disabled).toBe(false);
    });

    it("the gray recap box shows the chosen time and timezone before confirming", async () => {
      fetchSlotsMock.mockResolvedValueOnce({ ok: true, slots: [SLOT_A] });

      act(() => {
        root.render(<ScheduleCta config={baseConfig} summary={SUMMARY} />);
      });
      await flush();
      act(() => {
        getEnabledCalendarDayButton().click();
      });
      await flush();
      act(() => {
        getSlotButton(0).click();
      });
      continueFromTimeSelection();

      const recap = container.querySelector(".cw-sched-recap");
      expect(recap).not.toBeNull();
      expect(recap?.textContent).toMatch(/Time/i);
      expect(recap?.textContent).toMatch(/Date/i);
    });

    it("existingBooking non-null shows the 'keep it / book another' ask BEFORE the calendar; 'keep it' dismisses without booking; 'book another' proceeds", async () => {
      const summaryWithBooking: AvailabilitySummary = {
        ...SUMMARY,
        existingBooking: { startsAt: "2026-07-19T09:00:00+00:00", endsAt: "2026-07-19T09:30:00+00:00", timezone: "UTC" },
      };

      act(() => {
        root.render(<ScheduleCta config={baseConfig} summary={summaryWithBooking} />);
      });
      await flush();

      expect(container.querySelector('[role="grid"]')).toBeNull();
      const askText = container.textContent ?? "";
      expect(askText).toMatch(/already booked/i);

      const keepButton = container.querySelector<HTMLButtonElement>(".cw-sched-back-button");
      expect(keepButton?.textContent).toMatch(/keep it/i);

      act(() => {
        keepButton?.click();
      });
      // Dismissed: no calendar, no booking attempt.
      expect(container.querySelector('[role="grid"]')).toBeNull();
      expect(bookSlotMock).not.toHaveBeenCalled();
    });

    it("'book another' on the existing-booking ask proceeds into the calendar", async () => {
      const summaryWithBooking: AvailabilitySummary = {
        ...SUMMARY,
        existingBooking: { startsAt: "2026-07-19T09:00:00+00:00", endsAt: "2026-07-19T09:30:00+00:00", timezone: "UTC" },
      };

      act(() => {
        root.render(<ScheduleCta config={baseConfig} summary={summaryWithBooking} />);
      });
      await flush();

      const bookAnotherButton = container.querySelector<HTMLButtonElement>(".cw-sched-confirm-button");
      expect(bookAnotherButton?.textContent).toMatch(/book another/i);

      act(() => {
        bookAnotherButton?.click();
      });

      expect(container.querySelector('[role="grid"]')).not.toBeNull();
    });

    it("A11y: focus moves onto the first enabled calendar day when the calendar step mounts", async () => {
      act(() => {
        root.render(<ScheduleCta config={baseConfig} summary={SUMMARY} />);
      });
      await flush();

      const enabledDay = getEnabledCalendarDayButton();
      expect(document.activeElement).toBe(enabledDay);
    });

    it("A11y: focus moves onto the existing-booking ask heading when it mounts", async () => {
      const summaryWithBooking: AvailabilitySummary = {
        ...SUMMARY,
        existingBooking: { startsAt: "2026-07-19T09:00:00+00:00", endsAt: "2026-07-19T09:30:00+00:00", timezone: "UTC" },
      };

      act(() => {
        root.render(<ScheduleCta config={baseConfig} summary={summaryWithBooking} />);
      });
      await flush();

      const heading = container.querySelector("p[tabindex='-1']");
      expect(heading).not.toBeNull();
      expect(document.activeElement).toBe(heading);
    });

    it("SLOT_UNAVAILABLE on the staged flow re-fetches the SAME chosen day, never fabricates a confirmation", async () => {
      fetchSlotsMock.mockResolvedValueOnce({ ok: true, slots: [SLOT_A] });
      fetchSlotsMock.mockResolvedValueOnce({ ok: true, slots: [SLOT_B] });
      bookSlotMock.mockResolvedValueOnce({
        ok: false,
        error: {
          type: "SCHEDULE_ERROR",
          errorCode: "SLOT_UNAVAILABLE",
          message: "The requested time is no longer available.",
          correlationId: "corr-1",
          status: 422,
          retryAfterSeconds: null,
        },
      });
      vi.spyOn(console, "error").mockImplementation(() => {});

      act(() => {
        root.render(<ScheduleCta config={baseConfig} summary={SUMMARY} />);
      });
      await flush();
      act(() => {
        getEnabledCalendarDayButton().click();
      });
      await flush();
      act(() => {
        getSlotButton(0).click();
      });
      continueFromTimeSelection();
      act(() => {
        setNativeInputValue(getEmailInput(), "invite@example.com");
      });
      act(() => {
        getConsentCheckbox().click();
      });
      act(() => {
        getConfirmButton().click();
      });
      await flush();

      expect(fetchSlotsMock).toHaveBeenCalledTimes(2);
      const [, secondCallInput] = fetchSlotsMock.mock.calls[1] as [WidgetConfig, { dateFrom?: string; dateTo?: string }];
      expect(secondCallInput.dateFrom).toBe("2026-07-21");
      expect(container.querySelector(".cw-sched-confirmation")).toBeNull();
    });
  });
});

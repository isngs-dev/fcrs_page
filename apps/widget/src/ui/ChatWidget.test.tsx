import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { WidgetConfig } from "../config";
import type { TurnResult } from "../turn";
import type { FetchSlotsResult, FetchAvailabilitySummaryResult, PostHandoffIntentResult } from "../schedule";
import type { AdmissionResult } from "../session";
import type { IdentityResult } from "../identity";
import type { LeadResult } from "../lead";

// React 19's `act()` only batches/flushes updates when this flag is set —
// unlike mount.test.tsx's synchronous-only assertions, this suite drives
// state updates that occur across an `await` inside an event handler
// (the send flow), which requires the act environment to be declared.
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const sendTurnMock = vi.fn<(config: WidgetConfig, input: unknown) => Promise<TurnResult>>();
const submitIdentityMock = vi.fn<(config: WidgetConfig, input: unknown) => Promise<IdentityResult>>();
const fetchSlotsMock = vi.fn<(config: WidgetConfig, input: unknown) => Promise<FetchSlotsResult>>();
const fetchAvailabilitySummaryMock = vi.fn<(config: WidgetConfig) => Promise<FetchAvailabilitySummaryResult>>();
const postHandoffIntentMock = vi.fn<(config: WidgetConfig, input: { email: string }) => Promise<PostHandoffIntentResult>>();
const submitLeadMock = vi.fn<(config: WidgetConfig, input: unknown) => Promise<LeadResult>>();
const mintVisitorSessionMock = vi.fn<(config: WidgetConfig) => Promise<AdmissionResult>>();
const speakGreetingMock = vi.fn<(onBlocked?: () => void) => void>();
const ttsCancelMock = vi.fn<() => void>();
// SR-3: isResumeEnabled defaults false so every pre-existing test above
// (none of which opt into resume) sees byte-for-byte the same behavior —
// no touchResumeRecord call, no sessionStorage write.
const isResumeEnabledMock = vi.fn<() => boolean>(() => false);
const touchResumeRecordMock = vi.fn<(conversationId: string | null, now: Date) => void>();
const clearResumeRecordMock = vi.fn<() => void>();

vi.mock("../turn", () => ({
  sendTurn: (config: WidgetConfig, input: unknown) => sendTurnMock(config, input),
}));

// SR-14: mock identity.ts's submitIdentity so <IdentityForm> (rendered for
// action=identity_form) doesn't issue a real fetch here — identity.ts's own
// behavior is covered by identity.test.ts, and <IdentityForm>'s own
// rendering/a11y by IdentityForm.test.tsx. The consent constants are real
// (not mocked) so tests can assert on them if needed.
vi.mock("../identity", async () => {
  const actual = await vi.importActual<typeof import("../identity")>("../identity");
  return {
    ...actual,
    submitIdentity: (config: WidgetConfig, input: unknown) => submitIdentityMock(config, input),
  };
});

// S14.6: mock session's mintVisitorSession so the bounded expired-session
// re-mint (decision 5) can be asserted without a real fetch — the module
// also exports authHeader, which ChatWidget doesn't call directly, so it's
// omitted here. SR-3 adds isResumeEnabled (gates touchResumeRecord calls).
vi.mock("../session", () => ({
  mintVisitorSession: (config: WidgetConfig) => mintVisitorSessionMock(config),
  isResumeEnabled: () => isResumeEnabledMock(),
}));

// SR-3: mock resume.ts's write-side helpers so ChatWidget's touch/clear
// calls can be asserted without touching real sessionStorage here (resume.ts
// itself is covered by resume.test.ts).
vi.mock("../resume", () => ({
  touchResumeRecord: (conversationId: string | null, now: Date) => touchResumeRecordMock(conversationId, now),
  clearResumeRecord: () => clearResumeRecordMock(),
}));

// S14.5: mock the TTS module so ChatWidget's gesture-gating logic (only
// speak on the first open, only when not muted) can be asserted precisely
// without depending on jsdom having a real Web Speech API (it doesn't). The
// `onBlocked` callback is forwarded so tests can simulate Chrome's
// autoplay-policy block (see tts.ts) by invoking it themselves.
vi.mock("../tts", () => ({
  speakGreeting: (onBlocked?: () => void) => speakGreetingMock(onBlocked),
  cancel: () => ttsCancelMock(),
  TTS_GREETING_TEXT: "Hi! How can we help?",
}));

// ScheduleCta (rendered for action=schedule_cta, S14.4) calls fetchSlots on
// mount — mock it here too so this suite's schedule_cta test doesn't issue a
// real network call; ScheduleCta's own behavior is covered by ScheduleCta.test.tsx.
vi.mock("../schedule", async () => {
  const actual = await vi.importActual<typeof import("../schedule")>("../schedule");
  return {
    ...actual,
    fetchSlots: (config: WidgetConfig, input: unknown) => fetchSlotsMock(config, input),
    fetchAvailabilitySummary: (config: WidgetConfig) => fetchAvailabilitySummaryMock(config),
    postHandoffIntent: (config: WidgetConfig, input: { email: string }) => postHandoffIntentMock(config, input),
  };
});

// CalendlyHandoff (rendered for action=calendly_handoff, SR-6/SR-24) now
// also calls submitLead alongside postHandoffIntent so the visitor becomes
// a real Lead -- mock it here too so this suite's calendly_handoff tests
// don't issue a real network call; submitLead's own behavior is covered by
// lead.test.ts / LeadForm.test.tsx / CalendlyHandoff.test.tsx.
vi.mock("../lead", async () => {
  const actual = await vi.importActual<typeof import("../lead")>("../lead");
  return {
    ...actual,
    submitLead: (config: WidgetConfig, input: unknown) => submitLeadMock(config, input),
  };
});

import { ChatWidget } from "./ChatWidget";
import { widgetCss } from "./widgetCss";

const baseConfig: WidgetConfig = {
  clientKey: "pk_test_123",
  apiBase: "http://localhost:8000",
  mountSelector: null,
  debug: false,
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
  sendTurnMock.mockReset();
  submitIdentityMock.mockReset();
  fetchSlotsMock.mockReset();
  fetchSlotsMock.mockResolvedValue({ ok: true, slots: [] });
  fetchAvailabilitySummaryMock.mockReset();
  postHandoffIntentMock.mockReset();
  postHandoffIntentMock.mockResolvedValue({ ok: true, recorded: true });
  submitLeadMock.mockReset();
  submitLeadMock.mockResolvedValue({ ok: true, lead: { leadId: "lead-1", status: "new" } });
  mintVisitorSessionMock.mockReset();
  speakGreetingMock.mockReset();
  ttsCancelMock.mockReset();
  isResumeEnabledMock.mockReset();
  isResumeEnabledMock.mockReturnValue(false);
  touchResumeRecordMock.mockReset();
  clearResumeRecordMock.mockReset();
});

afterEach(() => {
  act(() => {
    root.unmount();
  });
  container.remove();
  vi.restoreAllMocks();
});

function getInput(): HTMLInputElement {
  const input = container.querySelector<HTMLInputElement>(".cw-input");
  if (!input) throw new Error("input not found");
  return input;
}

function getSendButton(): HTMLButtonElement {
  const button = container.querySelector<HTMLButtonElement>(".cw-send-button");
  if (!button) throw new Error("send button not found");
  return button;
}

/** Idempotent: the panel now opens automatically on mount, so most callers
 * just need "the panel to be open" and this is a no-op in that case. Tests
 * about the open/close TOGGLE itself use `closePanel()` + a direct
 * `launcher.click()` to exercise a real gesture. */
function openPanel(): void {
  const launcher = container.querySelector<HTMLButtonElement>(".cw-placeholder");
  if (!launcher) throw new Error("launcher not found");
  if (launcher.getAttribute("aria-expanded") === "true") return;
  act(() => {
    launcher.click();
  });
}

/** Idempotent counterpart to `openPanel()` -- closes via the launcher
 * (a real click gesture) if open, no-ops if already closed. Closing is now
 * a two-step exit-confirm interaction (click -> "Are you sure you want to
 * exit?" -> Yes), so this helper drives both steps transparently for every
 * existing call site that just wants "the panel is now closed and its chat
 * history cleared" as an end state. Tests about the confirmation step
 * itself (its exact message, the "No"/dismiss path, repeated-click
 * idempotency) exercise the two steps directly instead of using this. */
function closePanel(): void {
  const launcher = container.querySelector<HTMLButtonElement>(".cw-placeholder");
  if (!launcher) throw new Error("launcher not found");
  if (launcher.getAttribute("aria-expanded") === "false") return;
  act(() => {
    launcher.click();
  });
  const yesButton = container.querySelector<HTMLButtonElement>(".cw-confirm-close-yes");
  if (!yesButton) throw new Error("exit-confirm Yes button not found");
  act(() => {
    yesButton.click();
  });
}

// React tracks input values via the native <input> element's own property
// descriptor to detect "real" changes; a plain `input.value = text` write is
// invisible to React's synthetic event system in a jsdom environment. Go
// through the native setter (the same trick React Testing Library's
// `fireEvent`/`userEvent` use internally) so the subsequent "input" event
// is recognized and the controlled value actually updates.
function setNativeInputValue(input: HTMLInputElement, text: string): void {
  // The native property setter is read off the prototype and invoked via
  // Reflect.apply with an explicit `this` (input), so it is never actually
  // unbound; this is the standard React-testing trick for writing a "real"
  // value React's change detection will notice in a jsdom environment.
  // eslint-disable-next-line @typescript-eslint/unbound-method
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value")!.set!;
  Reflect.apply(setter, input, [text]);
}

function typeAndSend(text: string): void {
  const input = getInput();
  act(() => {
    setNativeInputValue(input, text);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
  act(() => {
    getSendButton().click();
  });
}

describe("ChatWidget", () => {
  it("renders a configured launcher label as literal text while preserving the launcher ARIA state", () => {
    act(() => {
      root.render(
        <ChatWidget
          config={baseConfig}
          expiresAt="2026-07-16T12:30:00Z"
          launcherLabel={'Chat <b>with us</b>'}
        />,
      );
    });

    const launcher = container.querySelector<HTMLButtonElement>(".cw-placeholder")!;
    // Opens automatically on mount -- no click required.
    expect(launcher.getAttribute("aria-label")).toBe("Close chat");
    expect(launcher.getAttribute("aria-expanded")).toBe("true");
    expect(launcher.textContent).toContain("Chat <b>with us</b>");
    expect(launcher.querySelector("b")).toBeNull();

    closePanel();
    expect(launcher.getAttribute("aria-label")).toBe("Open chat");
    expect(launcher.getAttribute("aria-expanded")).toBe("false");
  });

  it("uses the explicit default launcher label when no tenant label is supplied", () => {
    act(() => {
      root.render(<ChatWidget config={baseConfig} expiresAt="2026-07-16T12:30:00Z" />);
    });

    expect(container.querySelector(".cw-launcher-label")?.textContent).toBe("Ask Rebecca");
  });

  it("ships a self-contained floating Rebecca panel CSS contract", () => {
    expect(widgetCss).toContain("@font-face");
    expect(widgetCss).toContain("data:font/woff2;base64,");
    expect(widgetCss).toContain("@media (max-width: 480px)");
    expect(widgetCss).toContain("inset: 0");
    expect(widgetCss).not.toMatch(/https?:|\/\/fonts\./i);
    expect(widgetCss).not.toContain("Instrument Sans");
    expect(widgetCss).not.toContain("Inter");
  });

  it("opens automatically on mount, and the launcher still toggles it closed/open", () => {
    act(() => {
      root.render(<ChatWidget config={baseConfig} expiresAt="2026-07-16T12:30:00Z" />);
    });

    expect(container.querySelector(".cw-panel")).not.toBeNull();
    closePanel();
    expect(container.querySelector(".cw-panel")).toBeNull();
    openPanel();
    expect(container.querySelector(".cw-panel")).not.toBeNull();
  });

  it("starts a fresh local thread from the Rebecca header reset control", () => {
    act(() => {
      root.render(<ChatWidget config={baseConfig} expiresAt="2026-07-16T12:30:00Z" />);
    });
    openPanel();

    const input = getInput();
    act(() => {
      setNativeInputValue(input, "A draft question");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    expect(input.value).toBe("A draft question");

    const reset = container.querySelector<HTMLButtonElement>('.cw-reset-button[aria-label="Start a new chat"]');
    act(() => {
      reset?.click();
    });

    expect(input.value).toBe("");
    expect(container.querySelector(".cw-welcome")?.textContent).toMatch(/Rebecca/i);
    expect(clearResumeRecordMock).not.toHaveBeenCalled();
  });

  it("sending a message renders an optimistic user bubble + typing indicator, then a bot bubble; stores conversation_id for the next send", async () => {
    let resolveTurn: (value: TurnResult) => void = () => {};
    sendTurnMock.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveTurn = resolve;
        }),
    );

    act(() => {
      root.render(<ChatWidget config={baseConfig} expiresAt="2026-07-16T12:30:00Z" />);
    });
    openPanel();

    typeAndSend("Hello there");

    // Optimistic user bubble.
    expect(container.querySelector(".cw-bubble-row-user")?.textContent).toBe("Hello there");
    // Typing indicator visible while pending.
    expect(container.querySelector(".cw-typing")).not.toBeNull();
    // Input disabled while a turn is in flight.
    expect(getInput().disabled).toBe(true);

    await act(async () => {
      resolveTurn({
        ok: true,
        turn: {
          conversationId: "conv-99",
          messageId: "msg-1",
          reply: "Hi! How can I help?",
          decision: "answer",
          confidence: 0.9,
          sources: [],
          action: null,
        },
      });
      await Promise.resolve();
    });

    expect(container.querySelector(".cw-typing")).toBeNull();
    const botBubbles = container.querySelectorAll(".cw-bubble-row-bot .cw-bubble-bot");
    expect(botBubbles.length).toBe(1);
    expect(botBubbles[0]?.textContent).toContain("Hi! How can I help?");
    expect(getInput().disabled).toBe(false);

    // Second send must include the stored conversation_id.
    sendTurnMock.mockResolvedValueOnce({
      ok: true,
      turn: {
        conversationId: "conv-99",
        messageId: "msg-2",
        reply: "Sure thing.",
        decision: "answer",
        confidence: 0.9,
        sources: [],
        action: null,
      },
    });

    typeAndSend("Follow-up question");
    await flush();

    expect(sendTurnMock).toHaveBeenLastCalledWith(
      baseConfig,
      expect.objectContaining({ message: "Follow-up question", conversationId: "conv-99" }),
    );
  });

  it("a non-retryable turn failure renders a visible error line (not a bot bubble), re-enables input, and never fabricates a reply", async () => {
    // A 422 business error (not 5xx/429/network) is not retryable
    // (S14.6 decision 1/2) — exactly one attempt, immediately honest.
    sendTurnMock.mockResolvedValueOnce({
      ok: false,
      error: {
        type: "TURN_ERROR",
        errorCode: "VALIDATION_ERROR",
        message: "Invalid message.",
        correlationId: "corr-123",
        status: 422,
        retryAfterSeconds: null,
      },
    });
    const consoleErrorSpy = vi.spyOn(console, "error").mockImplementation(() => {});

    act(() => {
      root.render(<ChatWidget config={baseConfig} expiresAt="2026-07-16T12:30:00Z" />);
    });
    openPanel();

    typeAndSend("Will this fail?");
    await flush();

    const errorLine = container.querySelector(".cw-line-error");
    expect(errorLine).not.toBeNull();
    expect(errorLine?.textContent).toMatch(/something went wrong/i);

    // No bot bubble was fabricated.
    expect(container.querySelectorAll(".cw-bubble-row-bot .cw-bubble-bot").length).toBe(0);

    // Input re-enabled for manual retry.
    expect(getInput().disabled).toBe(false);

    expect(consoleErrorSpy).toHaveBeenCalledWith(expect.stringContaining("VALIDATION_ERROR"));
    expect(consoleErrorSpy).toHaveBeenCalledWith(expect.stringContaining("corr-123"));

    // No retry storm: exactly one call for the one send (non-retryable error).
    expect(sendTurnMock).toHaveBeenCalledTimes(1);
  });

  it("an escalation with action=lead_form first renders the human-handoff choice, then opens the authoritative lead form", async () => {
    sendTurnMock.mockResolvedValueOnce({
      ok: true,
      turn: {
        conversationId: "conv-1",
        messageId: "msg-1",
        reply: "I can connect you with a human.",
        decision: "escalate",
        confidence: 0.2,
        sources: [],
        action: "lead_form",
      },
    });

    act(() => {
      root.render(<ChatWidget config={baseConfig} expiresAt="2026-07-16T12:30:00Z" />);
    });
    openPanel();

    typeAndSend("I need help now");
    await flush();

    expect(container.querySelector(".cw-sched")).toBeNull();
    expect(container.querySelector(".cw-handoff-card")?.textContent).toContain("Connect with a rep");
    expect(container.querySelector("form.cw-lead-form")).toBeNull();

    fetchAvailabilitySummaryMock.mockResolvedValueOnce({
      ok: true,
      summary: {
        action: "lead_form",
        timezone: "UTC",
        days: [],
        transitionMessage: "Happy to connect you with a sales rep.",
        existingBooking: null,
      },
    });
    act(() => container.querySelector<HTMLButtonElement>(".cw-handoff-talk")?.click());
    await flush();

    expect(fetchAvailabilitySummaryMock).toHaveBeenCalledTimes(1);
    expect(container.querySelector("form.cw-lead-form")).not.toBeNull();
    expect(container.querySelector("input[type=email]")).not.toBeNull();
  });

  it("an escalation with action=schedule_cta opens the redesigned date picker only after Talk to a rep", async () => {
    sendTurnMock.mockResolvedValueOnce({
      ok: true,
      turn: {
        conversationId: "conv-1",
        messageId: "msg-1",
        reply: "Let's find a time.",
        decision: "escalate",
        confidence: 0.2,
        sources: [],
        action: "schedule_cta",
      },
    });

    act(() => {
      root.render(<ChatWidget config={baseConfig} expiresAt="2026-07-16T12:30:00Z" />);
    });
    openPanel();

    typeAndSend("Can we book a call?");
    await flush();

    expect(container.querySelector(".cw-handoff-card")).not.toBeNull();
    expect(container.querySelector(".cw-sched-day-strip")).toBeNull();
    fetchAvailabilitySummaryMock.mockResolvedValueOnce({
      ok: true,
      summary: {
        action: "schedule_cta",
        timezone: "UTC",
        days: [{ date: "2026-07-22", hasAvailability: true }],
        transitionMessage: "Happy to connect you with a sales rep. Please pick a time that works best for you.",
        existingBooking: null,
      },
    });
    act(() => container.querySelector<HTMLButtonElement>(".cw-handoff-talk")?.click());
    await flush();

    expect(fetchSlotsMock).not.toHaveBeenCalled();
    expect(container.querySelector(".cw-sched-day-strip")).not.toBeNull();
    expect(container.querySelector("form.cw-lead-form")).toBeNull();
    expect(container.querySelectorAll(".cw-bubble-row-user")[1]?.textContent).toBe("Talk to a rep");
  });

  it("Stay here keeps the visitor with Rebecca and does not open scheduling or call availability", async () => {
    sendTurnMock.mockResolvedValueOnce({
      ok: true,
      turn: {
        conversationId: "conv-1",
        messageId: "msg-1",
        reply: "I can connect you with a human.",
        decision: "escalate",
        confidence: 0.2,
        sources: [],
        action: "schedule_cta",
      },
    });

    act(() => root.render(<ChatWidget config={baseConfig} expiresAt="2026-07-16T12:30:00Z" />));
    openPanel();
    typeAndSend("I need help with a problem");
    await flush();

    act(() => container.querySelector<HTMLButtonElement>(".cw-handoff-stay")?.click());
    await flush();

    expect(fetchAvailabilitySummaryMock).not.toHaveBeenCalled();
    expect(container.querySelector(".cw-sched")).toBeNull();
    expect(container.textContent).toContain("Stay with Rebecca");
    expect(container.textContent).toContain("Can you tell me a bit more about what stopped working?");
    expect(container.querySelector<HTMLButtonElement>(".cw-handoff-stay")?.getAttribute("aria-pressed")).toBe("true");
  });

  describe("SR-14 conversation-start identity gate", () => {
    function getIdentityNameInput(): HTMLInputElement {
      const input = container.querySelector<HTMLInputElement>("#cw-identity-name");
      if (!input) throw new Error("identity name input not found");
      return input;
    }

    function getIdentityEmailInput(): HTMLInputElement {
      const input = container.querySelector<HTMLInputElement>("#cw-identity-email");
      if (!input) throw new Error("identity email input not found");
      return input;
    }

    function getIdentityConsentCheckbox(): HTMLInputElement {
      const input = container.querySelector<HTMLInputElement>("#cw-identity-consent");
      if (!input) throw new Error("identity consent checkbox not found");
      return input;
    }

    function getIdentitySubmitButton(): HTMLButtonElement {
      const button = container.querySelector<HTMLButtonElement>(".cw-identity-submit");
      if (!button) throw new Error("identity submit button not found");
      return button;
    }

    function fillAndSubmitIdentityForm(): void {
      act(() => {
        setNativeInputValue(getIdentityNameInput(), "Dana");
        getIdentityNameInput().dispatchEvent(new Event("input", { bubbles: true }));
        setNativeInputValue(getIdentityEmailInput(), "dana@example.com");
        getIdentityEmailInput().dispatchEvent(new Event("input", { bubbles: true }));
      });
      act(() => {
        getIdentityConsentCheckbox().click();
      });
      act(() => {
        getIdentitySubmitButton().click();
      });
    }

    it("a decision=identity_gate response renders the real IdentityForm, not LeadForm", async () => {
      sendTurnMock.mockResolvedValueOnce({
        ok: true,
        turn: {
          conversationId: "conv-1",
          messageId: "msg-1",
          reply: "Before I answer, could you share your name and email?",
          decision: "identity_gate",
          confidence: null,
          sources: [],
          action: "identity_form",
        },
      });

      act(() => {
        root.render(<ChatWidget config={baseConfig} expiresAt="2026-07-16T12:30:00Z" />);
      });
      openPanel();

      typeAndSend("How much does it cost?");
      await flush();

      expect(container.querySelector("form.cw-identity-form")).not.toBeNull();
      expect(container.querySelector("form.cw-lead-form")).toBeNull();
    });

    it("on successful capture, the deferred original question is auto-re-sent exactly once with no further visitor action, and answered as the next bot bubble", async () => {
      sendTurnMock.mockResolvedValueOnce({
        ok: true,
        turn: {
          conversationId: "conv-1",
          messageId: "msg-1",
          reply: "Before I answer, could you share your name and email?",
          decision: "identity_gate",
          confidence: null,
          sources: [],
          action: "identity_form",
        },
      });
      sendTurnMock.mockResolvedValueOnce({
        ok: true,
        turn: {
          conversationId: "conv-1",
          messageId: "msg-2",
          reply: "It costs $99/month.",
          decision: "answer",
          confidence: 0.9,
          sources: [],
          action: null,
        },
      });
      submitIdentityMock.mockResolvedValueOnce({ ok: true, identity: { leadId: "lead-1", status: "new" } });

      act(() => {
        root.render(<ChatWidget config={baseConfig} expiresAt="2026-07-16T12:30:00Z" />);
      });
      openPanel();

      typeAndSend("How much does it cost?");
      await flush();

      // Exactly one user bubble so far (the original question) -- the gate
      // reply is not a duplicate ask.
      const userBubblesBeforeCapture = container.querySelectorAll(".cw-bubble-row-user");
      expect(userBubblesBeforeCapture).toHaveLength(1);

      fillAndSubmitIdentityForm();
      await flush();
      await flush();

      expect(submitIdentityMock).toHaveBeenCalledTimes(1);
      // The deferred question was re-sent as the SECOND sendTurn call, on
      // the same conversation_id, with no visitor action beyond submitting
      // the identity form.
      expect(sendTurnMock).toHaveBeenCalledTimes(2);
      const secondCallInput = sendTurnMock.mock.calls[1]![1] as { message: string; conversationId: string | null };
      expect(secondCallInput.message).toBe("How much does it cost?");
      expect(secondCallInput.conversationId).toBe("conv-1");

      // No duplicate user bubble was appended by the auto-re-send.
      const userBubblesAfterCapture = container.querySelectorAll(".cw-bubble-row-user");
      expect(userBubblesAfterCapture).toHaveLength(1);

      // The real answer arrives as the next bot bubble.
      const botBubbles = container.querySelectorAll(".cw-bubble-bot");
      const lastBotBubbleText = botBubbles[botBubbles.length - 1]?.textContent ?? "";
      expect(lastBotBubbleText).toContain("$99/month");
    });

    it("on failed capture, an honest error shows and nothing is auto-re-sent", async () => {
      sendTurnMock.mockResolvedValueOnce({
        ok: true,
        turn: {
          conversationId: "conv-1",
          messageId: "msg-1",
          reply: "Before I answer, could you share your name and email?",
          decision: "identity_gate",
          confidence: null,
          sources: [],
          action: "identity_form",
        },
      });
      submitIdentityMock.mockResolvedValueOnce({
        ok: false,
        error: {
          type: "IDENTITY_ERROR",
          errorCode: "LLM_ERROR",
          message: "Backend failed.",
          correlationId: "corr-1",
          status: 502,
          retryAfterSeconds: null,
        },
      });
      vi.spyOn(console, "error").mockImplementation(() => {});

      act(() => {
        root.render(<ChatWidget config={baseConfig} expiresAt="2026-07-16T12:30:00Z" />);
      });
      openPanel();

      typeAndSend("How much does it cost?");
      await flush();

      fillAndSubmitIdentityForm();
      await flush();

      expect(container.querySelector(".cw-identity-error")).not.toBeNull();
      // No re-send: sendTurn was called exactly once (the original gated turn).
      expect(sendTurnMock).toHaveBeenCalledTimes(1);
    });
  });

  describe("S14.5 focus management + live region + TTS gesture gating", () => {
    it("is open with focus in it (the message input) immediately on mount, and re-opening after a close moves focus back in", () => {
      act(() => {
        root.render(<ChatWidget config={baseConfig} expiresAt="2026-07-16T12:30:00Z" />);
      });

      const launcher = container.querySelector<HTMLButtonElement>(".cw-placeholder")!;
      expect(launcher.getAttribute("aria-expanded")).toBe("true");
      expect(document.activeElement).toBe(getInput());

      closePanel();
      openPanel();

      expect(launcher.getAttribute("aria-expanded")).toBe("true");
      expect(document.activeElement).toBe(getInput());
    });

    it("the dialog has aria-labelledby resolving to the header text", () => {
      act(() => {
        root.render(<ChatWidget config={baseConfig} expiresAt="2026-07-16T12:30:00Z" />);
      });
      openPanel();

      const panel = container.querySelector<HTMLDivElement>(".cw-panel")!;
      const labelledBy = panel.getAttribute("aria-labelledby");
      expect(labelledBy).toBeTruthy();
      const headerEl = container.querySelector(`#${labelledBy}`);
      expect(headerEl).not.toBeNull();
      expect(headerEl?.textContent).toBe("Rebecca · AI assistant");
    });

    it("renders the first-open greeting and sends a selected suggestion through the real turn path", async () => {
      sendTurnMock.mockResolvedValueOnce({
        ok: true,
        turn: {
          conversationId: "conv-suggestion",
          messageId: "msg-suggestion",
          reply: "Here is the product overview.",
          decision: "answer",
          confidence: 0.9,
          sources: [],
          action: null,
        },
      });

      act(() => {
        root.render(<ChatWidget config={baseConfig} expiresAt="2026-07-16T12:30:00Z" />);
      });
      openPanel();

      expect(container.querySelector(".cw-welcome")?.textContent).toMatch(/Rebecca/i);
      const suggestion = Array.from(container.querySelectorAll<HTMLButtonElement>(".cw-suggestion")).find(
        (button) => button.textContent?.includes("Do you offer free roof inspections?"),
      );
      expect(suggestion).toBeDefined();

      act(() => {
        suggestion?.click();
      });
      await flush();

      expect(sendTurnMock).toHaveBeenCalledWith(
        baseConfig,
        expect.objectContaining({ message: "Do you offer free roof inspections?", conversationId: null }),
      );
      expect(container.querySelector(".cw-welcome")).not.toBeNull();
      expect(container.querySelector(".cw-suggestion-selected")?.textContent).toContain("Do you offer free roof inspections?");
      expect(container.querySelector(".cw-bubble-row-user")?.textContent).toBe("Do you offer free roof inspections?");
    });

    it("uses browser speech recognition when available and stops it when the panel closes", () => {
      type ResultHandler = (event: { results: ArrayLike<{ 0: { transcript: string } }> }) => void;

      class FakeRecognition {
        static latest: FakeRecognition | null = null;
        continuous = true;
        interimResults = true;
        lang = "";
        onstart: (() => void) | null = null;
        onend: (() => void) | null = null;
        onerror: (() => void) | null = null;
        onresult: ResultHandler | null = null;
        start = vi.fn(() => this.onstart?.());
        stop = vi.fn(() => this.onend?.());
        abort = vi.fn();

        constructor() {
          FakeRecognition.latest = this;
        }
      }

      Object.defineProperty(window, "webkitSpeechRecognition", {
        configurable: true,
        value: FakeRecognition,
      });

      try {
        act(() => {
          root.render(<ChatWidget config={baseConfig} expiresAt="2026-07-16T12:30:00Z" />);
        });
        openPanel();

        const voiceButton = container.querySelector<HTMLButtonElement>('.cw-voice-button[aria-label="Start voice input"]');
        expect(voiceButton).not.toBeNull();
        act(() => {
          voiceButton?.click();
        });
        expect(FakeRecognition.latest).not.toBeNull();
        expect(voiceButton?.getAttribute("aria-pressed")).toBe("true");

        act(() => {
          FakeRecognition.latest?.onresult?.({ results: [{ 0: { transcript: "Book a demo" } }] });
        });
        expect(getInput().value).toBe("Book a demo");

        const closeButton = container.querySelector<HTMLButtonElement>('.cw-close-button[aria-label="Close chat"]');
        act(() => {
          closeButton?.click();
        });
        expect(FakeRecognition.latest?.stop).toHaveBeenCalled();
      } finally {
        Reflect.deleteProperty(window, "webkitSpeechRecognition");
      }
    });

    it("does not render a dead voice control when speech recognition is unavailable", () => {
      act(() => {
        root.render(<ChatWidget config={baseConfig} expiresAt="2026-07-16T12:30:00Z" />);
      });
      openPanel();

      expect(container.querySelector(".cw-voice-button")).toBeNull();
    });

    it("has an in-panel close control that shows the exit confirmation, then closes + restores launcher focus on Yes", () => {
      act(() => {
        root.render(<ChatWidget config={baseConfig} expiresAt="2026-07-16T12:30:00Z" />);
      });
      openPanel();

      const closeButton = container.querySelector<HTMLButtonElement>('.cw-close-button[aria-label="Close chat"]');
      expect(closeButton).not.toBeNull();
      act(() => {
        closeButton?.click();
      });

      // Clicking X no longer closes immediately -- it shows the exit
      // confirmation in place of the normal panel body; the panel itself
      // stays mounted throughout.
      expect(container.querySelector(".cw-panel")).not.toBeNull();
      expect(container.querySelector(".cw-panel-header")).toBeNull();
      const yesButton = container.querySelector<HTMLButtonElement>(".cw-confirm-close-yes");
      expect(yesButton).not.toBeNull();

      act(() => {
        yesButton?.click();
      });

      const launcher = container.querySelector<HTMLButtonElement>(".cw-placeholder")!;
      expect(container.querySelector(".cw-panel")).toBeNull();
      expect(document.activeElement).toBe(launcher);
    });

    it("Escape shows the exit confirmation (not an immediate close); a second Escape dismisses it and keeps the panel open", () => {
      act(() => {
        root.render(<ChatWidget config={baseConfig} expiresAt="2026-07-16T12:30:00Z" />);
      });
      openPanel();
      expect(container.querySelector(".cw-panel")).not.toBeNull();

      const panel = container.querySelector<HTMLDivElement>(".cw-panel")!;
      act(() => {
        panel.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
      });

      // First Escape: shows the confirmation, panel stays open/mounted.
      expect(container.querySelector(".cw-confirm-close")).not.toBeNull();
      expect(container.querySelector(".cw-panel")).not.toBeNull();

      // Second Escape, while confirming: acts as "No" -- dismisses the
      // confirmation and returns to the normal (still-open) panel view.
      act(() => {
        panel.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
      });
      expect(container.querySelector(".cw-confirm-close")).toBeNull();
      expect(container.querySelector(".cw-panel-header")).not.toBeNull();
      expect(container.querySelector(".cw-panel")).not.toBeNull();
    });

    it("Escape then Yes (via click) closes the panel and restores focus to the launcher", () => {
      act(() => {
        root.render(<ChatWidget config={baseConfig} expiresAt="2026-07-16T12:30:00Z" />);
      });
      openPanel();

      const panel = container.querySelector<HTMLDivElement>(".cw-panel")!;
      act(() => {
        panel.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
      });
      const yesButton = container.querySelector<HTMLButtonElement>(".cw-confirm-close-yes")!;
      act(() => {
        yesButton.click();
      });

      expect(container.querySelector(".cw-panel")).toBeNull();
      const launcher = container.querySelector<HTMLButtonElement>(".cw-placeholder")!;
      expect(document.activeElement).toBe(launcher);
    });

    it("Tab at the last focusable element wraps to the first (focus trap)", () => {
      act(() => {
        root.render(<ChatWidget config={baseConfig} expiresAt="2026-07-16T12:30:00Z" />);
      });
      openPanel();

      const panel = container.querySelector<HTMLDivElement>(".cw-panel")!;
      const focusable = Array.from(
        panel.querySelectorAll<HTMLElement>("a[href],button:not([disabled]),input:not([disabled])"),
      );
      const last = focusable[focusable.length - 1]!;
      const first = focusable[0]!;

      last.focus();
      expect(document.activeElement).toBe(last);

      act(() => {
        panel.dispatchEvent(new KeyboardEvent("keydown", { key: "Tab", bubbles: true, cancelable: true }));
      });

      expect(document.activeElement).toBe(first);
    });

    it("Shift+Tab at the first focusable element wraps to the last (reverse trap)", () => {
      act(() => {
        root.render(<ChatWidget config={baseConfig} expiresAt="2026-07-16T12:30:00Z" />);
      });
      openPanel();

      const panel = container.querySelector<HTMLDivElement>(".cw-panel")!;
      const focusable = Array.from(
        panel.querySelectorAll<HTMLElement>("a[href],button:not([disabled]),input:not([disabled])"),
      );
      const first = focusable[0]!;
      const last = focusable[focusable.length - 1]!;

      first.focus();
      expect(document.activeElement).toBe(first);

      act(() => {
        panel.dispatchEvent(
          new KeyboardEvent("keydown", { key: "Tab", shiftKey: true, bubbles: true, cancelable: true }),
        );
      });

      expect(document.activeElement).toBe(last);
    });

    it("the message list carries aria-live=polite + aria-relevant=additions; typing indicator stays aria-live=off", async () => {
      let resolveTurn: (value: TurnResult) => void = () => {};
      sendTurnMock.mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveTurn = resolve;
          }),
      );

      act(() => {
        root.render(<ChatWidget config={baseConfig} expiresAt="2026-07-16T12:30:00Z" />);
      });
      openPanel();

      const list = container.querySelector(".cw-message-list")!;
      expect(list.getAttribute("aria-live")).toBe("polite");
      expect(list.getAttribute("aria-relevant")).toBe("additions");

      typeAndSend("Hello");

      const typingRow = container.querySelector(".cw-bubble-row .cw-typing")?.closest(".cw-bubble-row");
      expect(typingRow?.getAttribute("aria-live")).toBe("off");

      await act(async () => {
        resolveTurn({
          ok: true,
          turn: {
            conversationId: "conv-1",
            messageId: "msg-1",
            reply: "Hi!",
            decision: "answer",
            confidence: 0.9,
            sources: [],
            action: null,
          },
        });
        await Promise.resolve();
      });
    });

    it("TTS: speaks exactly once automatically on the mount-time auto-open, when not muted", () => {
      act(() => {
        root.render(<ChatWidget config={baseConfig} expiresAt="2026-07-16T12:30:00Z" />);
      });

      // The panel opens automatically on mount, and the greeting is
      // attempted right along with it (user request) -- no click required.
      expect(speakGreetingMock).toHaveBeenCalledTimes(1);

      // Closing and reopening again in the same page session must not speak again.
      closePanel();
      openPanel();
      expect(speakGreetingMock).toHaveBeenCalledTimes(1);
    });

    it("TTS: re-opening after muting does not speak again (mute suppresses future opens)", () => {
      act(() => {
        root.render(<ChatWidget config={baseConfig} expiresAt="2026-07-16T12:30:00Z" />);
      });

      // Mount-time auto-open already greeted once.
      expect(speakGreetingMock).toHaveBeenCalledTimes(1);

      const muteToggle = container.querySelector<HTMLButtonElement>(".cw-mute-toggle")!;
      act(() => {
        muteToggle.click();
      });

      closePanel();
      openPanel(); // reopen while muted
      // Greeting only ever fires once per page session (decision 5), so
      // this also confirms mute doesn't retroactively matter for the
      // already-consumed mount-time call — the important invariant is no
      // *additional* speak call happens.
      expect(speakGreetingMock).toHaveBeenCalledTimes(1);
    });

    it("TTS: the mute toggle mutes future opens and is visible with aria-pressed", () => {
      // Mount-time auto-open already consumes the once-per-session
      // greeting; mute, close, reopen — no further speak calls, and the
      // toggle communicates state via aria-pressed.
      act(() => {
        root.render(<ChatWidget config={baseConfig} expiresAt="2026-07-16T12:30:00Z" />);
      });
      expect(speakGreetingMock).toHaveBeenCalledTimes(1);

      const muteToggle = container.querySelector<HTMLButtonElement>(".cw-mute-toggle")!;
      expect(muteToggle.getAttribute("aria-pressed")).toBe("false");

      act(() => {
        muteToggle.click();
      });
      expect(muteToggle.getAttribute("aria-pressed")).toBe("true");
      expect(ttsCancelMock).toHaveBeenCalled();
    });

    it("TTS: retries on the visitor's first interaction anywhere on the page after a blocked mount-time attempt", () => {
      // Simulate Chrome's "no speak() without prior user activation"
      // autoplay policy: the mount-time attempt is blocked (onBlocked fires
      // synchronously here in place of the utterance's real async onerror,
      // see tts.ts) -- the retry after a real interaction then succeeds,
      // matching Chrome's actual behavior once activation has been granted.
      speakGreetingMock.mockImplementationOnce((onBlocked) => onBlocked?.());

      act(() => {
        root.render(<ChatWidget config={baseConfig} expiresAt="2026-07-16T12:30:00Z" />);
      });
      // Mount-time attempt, blocked.
      expect(speakGreetingMock).toHaveBeenCalledTimes(1);

      // The visitor's first interaction anywhere on the page -- not the
      // widget itself -- grants the activation Chrome requires and should
      // trigger a genuine retry.
      act(() => {
        document.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, cancelable: true }));
      });
      expect(speakGreetingMock).toHaveBeenCalledTimes(2);

      // That retry succeeded (no onBlocked this time), so a further
      // interaction must not speak a third time.
      act(() => {
        document.dispatchEvent(new TouchEvent("touchstart", { bubbles: true, cancelable: true }));
      });
      expect(speakGreetingMock).toHaveBeenCalledTimes(2);
    });

    it("TTS: does not retry on interaction once the mount-time attempt genuinely spoke", () => {
      act(() => {
        root.render(<ChatWidget config={baseConfig} expiresAt="2026-07-16T12:30:00Z" />);
      });
      expect(speakGreetingMock).toHaveBeenCalledTimes(1);

      act(() => {
        document.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
      });
      expect(speakGreetingMock).toHaveBeenCalledTimes(1);
    });

    it("TTS: a first-interaction retry does not fire once muted", () => {
      speakGreetingMock.mockImplementation((onBlocked) => onBlocked?.());
      act(() => {
        root.render(<ChatWidget config={baseConfig} expiresAt="2026-07-16T12:30:00Z" />);
      });
      expect(speakGreetingMock).toHaveBeenCalledTimes(1);

      const muteToggle = container.querySelector<HTMLButtonElement>(".cw-mute-toggle")!;
      act(() => {
        muteToggle.click();
      });
      // The mute click itself is a real page interaction and may already
      // trigger one more (still-blocked) attempt before the `muted` state
      // update lands -- assert relative to that, not a fixed count, so this
      // test is only about what happens AFTER muting has genuinely landed.
      const callsAfterMuting = speakGreetingMock.mock.calls.length;

      act(() => {
        document.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, cancelable: true }));
      });
      expect(speakGreetingMock).toHaveBeenCalledTimes(callsAfterMuting);
    });
  });

  describe("exit confirmation on close (X) — clears client-side chat history, never admin data", () => {
    it("clicking the close (X) button shows the exact confirmation message and does not close the panel", () => {
      act(() => {
        root.render(<ChatWidget config={baseConfig} expiresAt="2026-07-16T12:30:00Z" />);
      });
      openPanel();

      const closeButton = container.querySelector<HTMLButtonElement>(".cw-close-button")!;
      act(() => {
        closeButton.click();
      });

      expect(container.querySelector(".cw-confirm-close-message")?.textContent).toBe(
        "Are you sure you want to exit?",
      );
      // Still open -- the confirmation replaces the panel body, it does not
      // close the panel by itself.
      expect(container.querySelector(".cw-panel")).not.toBeNull();
    });

    it("focus lands on 'No' (the safe/non-destructive option) when the confirmation appears", () => {
      act(() => {
        root.render(<ChatWidget config={baseConfig} expiresAt="2026-07-16T12:30:00Z" />);
      });
      openPanel();

      act(() => {
        container.querySelector<HTMLButtonElement>(".cw-close-button")!.click();
      });

      expect(document.activeElement).toBe(container.querySelector(".cw-confirm-close-no"));
    });

    it("repeated close clicks cannot stack multiple confirmations -- the close button is replaced by the dialog, not layered on top of it", () => {
      act(() => {
        root.render(<ChatWidget config={baseConfig} expiresAt="2026-07-16T12:30:00Z" />);
      });
      openPanel();

      act(() => {
        container.querySelector<HTMLButtonElement>(".cw-close-button")!.click();
      });
      expect(container.querySelectorAll(".cw-confirm-close").length).toBe(1);
      // The trigger itself is gone now (replaced by the confirm view), so a
      // "repeated click" on it is structurally impossible via the UI --
      // there is nothing left to click that could show a second dialog.
      expect(container.querySelector(".cw-close-button")).toBeNull();
    });

    it("'No' keeps the panel open and preserves chat history + the conversation id exactly as it was", async () => {
      sendTurnMock
        .mockResolvedValueOnce({
          ok: true,
          turn: {
            conversationId: "conv-keep-me",
            messageId: "msg-1",
            reply: "Sure, here you go.",
            decision: "answer",
            confidence: 0.9,
            sources: [],
            action: null,
          },
        })
        .mockResolvedValueOnce({
          ok: true,
          turn: {
            conversationId: "conv-keep-me",
            messageId: "msg-2",
            reply: "Still here.",
            decision: "answer",
            confidence: 0.9,
            sources: [],
            action: null,
          },
        });

      act(() => {
        root.render(<ChatWidget config={baseConfig} expiresAt="2026-07-16T12:30:00Z" />);
      });
      openPanel();
      typeAndSend("Hello there");
      await flush();
      expect(container.querySelector(".cw-bubble-row-user")?.textContent).toBe("Hello there");

      act(() => {
        container.querySelector<HTMLButtonElement>(".cw-close-button")!.click();
      });
      act(() => {
        container.querySelector<HTMLButtonElement>(".cw-confirm-close-no")!.click();
      });

      // Back to the normal view, panel still open, nothing cleared.
      expect(container.querySelector(".cw-confirm-close")).toBeNull();
      expect(container.querySelector(".cw-panel")).not.toBeNull();
      expect(container.querySelector(".cw-bubble-row-user")?.textContent).toBe("Hello there");
      expect(container.querySelector(".cw-bubble-row-bot")?.textContent).toContain("Sure, here you go.");
      // A follow-up send continues the SAME conversation -- confirms
      // conversationIdRef was never touched by "No".
      typeAndSend("Follow-up");
      await flush();
      expect(sendTurnMock).toHaveBeenLastCalledWith(
        baseConfig,
        expect.objectContaining({ message: "Follow-up", conversationId: "conv-keep-me" }),
      );
      // Declining the exit is a purely local UI decision -- resume.ts is
      // never touched by "No".
      expect(clearResumeRecordMock).not.toHaveBeenCalled();
    });

    it("dismissing via Escape while the confirmation is showing behaves exactly like 'No' -- history preserved, focus returns to the input", async () => {
      sendTurnMock.mockResolvedValueOnce({
        ok: true,
        turn: {
          conversationId: "conv-1",
          messageId: "msg-1",
          reply: "Sure.",
          decision: "answer",
          confidence: 0.9,
          sources: [],
          action: null,
        },
      });

      act(() => {
        root.render(<ChatWidget config={baseConfig} expiresAt="2026-07-16T12:30:00Z" />);
      });
      openPanel();
      typeAndSend("Keep this");
      await flush();

      const panel = container.querySelector<HTMLDivElement>(".cw-panel")!;
      act(() => {
        panel.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
      });
      expect(container.querySelector(".cw-confirm-close")).not.toBeNull();

      act(() => {
        panel.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
      });

      expect(container.querySelector(".cw-confirm-close")).toBeNull();
      expect(container.querySelector(".cw-bubble-row-user")?.textContent).toBe("Keep this");
      expect(document.activeElement).toBe(getInput());
    });

    it("'Yes' closes the panel, clears the visible message history, and the next reopen starts a completely fresh chat (no previous messages, new conversation_id)", async () => {
      sendTurnMock
        .mockResolvedValueOnce({
          ok: true,
          turn: {
            conversationId: "conv-old",
            messageId: "msg-1",
            reply: "First reply.",
            decision: "answer",
            confidence: 0.9,
            sources: [],
            action: null,
          },
        })
        .mockResolvedValueOnce({
          ok: true,
          turn: {
            conversationId: "conv-new",
            messageId: "msg-2",
            reply: "Second reply.",
            decision: "answer",
            confidence: 0.9,
            sources: [],
            action: null,
          },
        });

      act(() => {
        root.render(<ChatWidget config={baseConfig} expiresAt="2026-07-16T12:30:00Z" />);
      });
      openPanel();
      typeAndSend("Old conversation");
      await flush();
      expect(container.querySelector(".cw-bubble-row-user")).not.toBeNull();

      act(() => {
        container.querySelector<HTMLButtonElement>(".cw-close-button")!.click();
      });
      act(() => {
        container.querySelector<HTMLButtonElement>(".cw-confirm-close-yes")!.click();
      });

      // Actually closed now, focus restored to the launcher (S14.5's
      // existing close-focus-restore behavior, unchanged for the Yes path).
      expect(container.querySelector(".cw-panel")).toBeNull();
      const launcher = container.querySelector<HTMLButtonElement>(".cw-placeholder")!;
      expect(document.activeElement).toBe(launcher);

      openPanel();
      // No previous messages visible -- the message list is back to the
      // fresh-open welcome state, not "Old conversation" / "First reply.".
      expect(container.querySelector(".cw-bubble-row-user")).toBeNull();
      expect(container.querySelector(".cw-bubble-row-bot")).toBeNull();

      typeAndSend("New conversation");
      await flush();
      // The NEXT turn carries conversationId: null (a brand-new thread),
      // never "conv-old" -- proves conversationIdRef was actually cleared,
      // not just the visible bubbles.
      expect(sendTurnMock).toHaveBeenLastCalledWith(
        baseConfig,
        expect.objectContaining({ message: "New conversation", conversationId: null }),
      );
    });

    it("'Yes' clears the SR-3 sessionStorage resume mirror only when the tenant has resume enabled -- mirrors the existing 'New chat' reset button's own opt-in gating", () => {
      isResumeEnabledMock.mockReturnValue(true);
      act(() => {
        root.render(<ChatWidget config={baseConfig} expiresAt="2026-07-16T12:30:00Z" />);
      });
      openPanel();

      act(() => {
        container.querySelector<HTMLButtonElement>(".cw-close-button")!.click();
      });
      act(() => {
        container.querySelector<HTMLButtonElement>(".cw-confirm-close-yes")!.click();
      });

      expect(clearResumeRecordMock).toHaveBeenCalledTimes(1);
    });

    it("'Yes' never calls any network function beyond what a normal turn/admission already uses -- no admin/delete endpoint is touched, so the admin dashboard's copy of this conversation is never affected", async () => {
      sendTurnMock.mockResolvedValueOnce({
        ok: true,
        turn: {
          conversationId: "conv-1",
          messageId: "msg-1",
          reply: "Hi!",
          decision: "answer",
          confidence: 0.9,
          sources: [],
          action: null,
        },
      });

      act(() => {
        root.render(<ChatWidget config={baseConfig} expiresAt="2026-07-16T12:30:00Z" />);
      });
      openPanel();
      typeAndSend("Hello");
      await flush();
      const sendTurnCallsBeforeClose = sendTurnMock.mock.calls.length;
      const mintCallsBeforeClose = mintVisitorSessionMock.mock.calls.length;

      act(() => {
        container.querySelector<HTMLButtonElement>(".cw-close-button")!.click();
      });
      act(() => {
        container.querySelector<HTMLButtonElement>(".cw-confirm-close-yes")!.click();
      });

      // Exiting is a purely client-side state clear: it issues no new
      // sendTurn or mintVisitorSession call, and (per this file's ../resume
      // mock) the ONLY resume.ts function it invokes is clearResumeRecord,
      // never anything that could reach a server-side delete/admin route --
      // nothing here is even capable of touching the conversation_store the
      // admin dashboard reads from.
      expect(sendTurnMock.mock.calls.length).toBe(sendTurnCallsBeforeClose);
      expect(mintVisitorSessionMock.mock.calls.length).toBe(mintCallsBeforeClose);
    });
  });

  describe("S14.6 retry/backoff, connection status, and bounded reconnect", () => {
    beforeEach(() => {
      vi.useFakeTimers();
    });

    afterEach(() => {
      vi.useRealTimers();
    });

    /** Advance the fake clock enough to flush withRetry's default setTimeout-based sleep between attempts. */
    async function advanceThroughBackoff(): Promise<void> {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(15000);
      });
    }

    it("a transient (network) turn failure triggers a bounded auto-retry with a visible retrying status, then an offline status + manual Retry, and never fabricates a reply", async () => {
      const failure: TurnResult = {
        ok: false,
        error: {
          type: "TURN_ERROR",
          errorCode: "NETWORK_ERROR",
          message: "Network request failed.",
          correlationId: null,
          status: null,
          retryAfterSeconds: null,
        },
      };
      sendTurnMock.mockResolvedValue(failure);
      vi.spyOn(console, "error").mockImplementation(() => {});

      act(() => {
        root.render(<ChatWidget config={baseConfig} expiresAt="2026-07-16T12:30:00Z" />);
      });
      openPanel();

      typeAndSend("Hello?");

      // Retrying status appears while attempts are still bounded and in progress.
      await act(async () => {
        await Promise.resolve();
      });
      expect(container.querySelector(".cw-status-text")?.textContent).toMatch(/reconnecting/i);

      await advanceThroughBackoff();

      // Bounded: default cap is 4 attempts — no more, no infinite loop.
      expect(sendTurnMock).toHaveBeenCalledTimes(4);

      // Honest offline status + manual Retry, no fabricated bot reply.
      expect(container.querySelector(".cw-status-text")?.textContent).toMatch(/can't reach chat/i);
      expect(container.querySelector(".cw-status-retry")).not.toBeNull();
      expect(container.querySelectorAll(".cw-bubble-row-bot .cw-bubble-bot").length).toBe(0);
      const errorLine = container.querySelector(".cw-line-error");
      expect(errorLine).not.toBeNull();
    });

    it("a 429 shows the rate-limited status and does not retry before the wait", async () => {
      const rateLimited: TurnResult = {
        ok: false,
        error: {
          type: "TURN_ERROR",
          errorCode: "RATE_LIMITED",
          message: "Too many requests.",
          correlationId: null,
          status: 429,
          retryAfterSeconds: 30,
        },
      };
      sendTurnMock.mockResolvedValue(rateLimited);
      vi.spyOn(console, "error").mockImplementation(() => {});

      act(() => {
        root.render(<ChatWidget config={baseConfig} expiresAt="2026-07-16T12:30:00Z" />);
      });
      openPanel();

      typeAndSend("Hello?");

      await act(async () => {
        await Promise.resolve();
      });

      // Rate-limited status shown; the retryAfterSeconds value is surfaced honestly.
      expect(container.querySelector(".cw-status-text")?.textContent).toMatch(/30s/);
      // Still only the first attempt — has not retried before the (30s) wait.
      expect(sendTurnMock).toHaveBeenCalledTimes(1);

      // Advancing less than the server-mandated wait must not trigger attempt 2.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(5000);
      });
      expect(sendTurnMock).toHaveBeenCalledTimes(1);
    });

    it("a 401 triggers the bounded re-mint reconnect (mintVisitorSession called at most the cap, not an unbounded loop) with an honest reconnecting status", async () => {
      const authFailure: TurnResult = {
        ok: false,
        error: {
          type: "TURN_ERROR",
          errorCode: "UNAUTHENTICATED",
          message: "Token expired.",
          correlationId: null,
          status: 401,
          retryAfterSeconds: null,
        },
      };
      // 401 is non-retryable at the transport layer (not in the retryable set), so
      // withRetry returns it after exactly one sendTurn attempt; ChatWidget's own
      // bounded re-mint sequence then kicks in.
      sendTurnMock.mockResolvedValueOnce(authFailure);
      mintVisitorSessionMock.mockResolvedValue({
        ok: false,
        error: {
          type: "ADMISSION_ERROR",
          errorCode: "NETWORK_ERROR",
          message: "Network request failed.",
          correlationId: null,
          status: null,
          retryAfterSeconds: null,
        },
      });
      vi.spyOn(console, "error").mockImplementation(() => {});

      act(() => {
        root.render(<ChatWidget config={baseConfig} expiresAt="2026-07-16T12:30:00Z" />);
      });
      openPanel();

      typeAndSend("Hello?");

      await act(async () => {
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
      });

      // Bounded: at most REMINT_MAX_ATTEMPTS (2) re-mint calls — never unbounded.
      expect(mintVisitorSessionMock.mock.calls.length).toBeLessThanOrEqual(2);
      expect(mintVisitorSessionMock.mock.calls.length).toBeGreaterThan(0);
      // On re-mint failure, an honest "please reload" state — not a silent retry loop.
      expect(container.querySelector(".cw-status-text")?.textContent).toMatch(/session expired/i);
    });

    it("a 401 followed by a successful re-mint shows an honest reconnected status without fabricating a reply", async () => {
      const authFailure: TurnResult = {
        ok: false,
        error: {
          type: "TURN_ERROR",
          errorCode: "UNAUTHENTICATED",
          message: "Token expired.",
          correlationId: null,
          status: 401,
          retryAfterSeconds: null,
        },
      };
      sendTurnMock.mockResolvedValueOnce(authFailure);
      mintVisitorSessionMock.mockResolvedValueOnce({
        ok: true,
        session: { visitorToken: "jwt.new", expiresAt: "2026-07-16T13:00:00Z" },
      });
      vi.spyOn(console, "error").mockImplementation(() => {});

      act(() => {
        root.render(<ChatWidget config={baseConfig} expiresAt="2026-07-16T12:30:00Z" />);
      });
      openPanel();

      typeAndSend("Hello?");

      await act(async () => {
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
      });

      expect(mintVisitorSessionMock).toHaveBeenCalledTimes(1);
      expect(container.querySelectorAll(".cw-bubble-row-bot .cw-bubble-bot").length).toBe(0);
      const errorLine = container.querySelector(".cw-line-error");
      expect(errorLine?.textContent).toMatch(/reconnected/i);
    });

    it("closing the panel mid-retry clears the timer — no further sendTurn fetch fires after close (zombie-storm guard)", async () => {
      const failure: TurnResult = {
        ok: false,
        error: {
          type: "TURN_ERROR",
          errorCode: "NETWORK_ERROR",
          message: "Network request failed.",
          correlationId: null,
          status: null,
          retryAfterSeconds: null,
        },
      };
      sendTurnMock.mockResolvedValue(failure);
      vi.spyOn(console, "error").mockImplementation(() => {});

      act(() => {
        root.render(<ChatWidget config={baseConfig} expiresAt="2026-07-16T12:30:00Z" />);
      });
      openPanel();

      typeAndSend("Hello?");

      // Let the first attempt fail and the retry timer get scheduled.
      await act(async () => {
        await Promise.resolve();
        await Promise.resolve();
      });
      const callsBeforeUnmount = sendTurnMock.mock.calls.length;
      expect(callsBeforeUnmount).toBeGreaterThan(0);

      // Unmount the whole component (simulates the widget panel/root being torn down).
      act(() => {
        root.unmount();
      });

      // Advance well past every remaining backoff window.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(30000);
      });

      // No further attempts after unmount — the shouldAbort guard stopped withRetry.
      expect(sendTurnMock.mock.calls.length).toBe(callsBeforeUnmount);
    });

    it("connection status uses the polite live region (role=status, aria-live=polite) — not assertive", () => {
      act(() => {
        root.render(<ChatWidget config={baseConfig} expiresAt="2026-07-16T12:30:00Z" />);
      });
      openPanel();

      const status = container.querySelector(".cw-status");
      expect(status?.getAttribute("role")).toBe("status");
      expect(status?.getAttribute("aria-live")).toBe("polite");
    });

    it("the manual Retry button replays the last failed send", async () => {
      const failure: TurnResult = {
        ok: false,
        error: {
          type: "TURN_ERROR",
          errorCode: "NETWORK_ERROR",
          message: "Network request failed.",
          correlationId: null,
          status: null,
          retryAfterSeconds: null,
        },
      };
      sendTurnMock.mockResolvedValue(failure);
      vi.spyOn(console, "error").mockImplementation(() => {});

      act(() => {
        root.render(<ChatWidget config={baseConfig} expiresAt="2026-07-16T12:30:00Z" />);
      });
      openPanel();

      typeAndSend("Hello?");
      await advanceThroughBackoff();

      const callsAfterAutoRetryExhausted = sendTurnMock.mock.calls.length;
      expect(callsAfterAutoRetryExhausted).toBe(4);

      sendTurnMock.mockResolvedValueOnce({
        ok: true,
        turn: {
          conversationId: "conv-retry",
          messageId: "msg-retry",
          reply: "Sorry about that — I'm here now.",
          decision: "answer",
          confidence: 0.9,
          sources: [],
          action: null,
        },
      });

      const retryButton = container.querySelector<HTMLButtonElement>(".cw-status-retry");
      expect(retryButton).not.toBeNull();
      act(() => {
        retryButton!.click();
      });
      await advanceThroughBackoff();

      expect(sendTurnMock.mock.calls.length).toBe(callsAfterAutoRetryExhausted + 1);
      const botBubbles = container.querySelectorAll(".cw-bubble-row-bot .cw-bubble-bot");
      expect(botBubbles.length).toBe(1);
      expect(botBubbles[0]?.textContent).toContain("Sorry about that");
    });
  });

  describe("SR-3: conversation continuity across reload", () => {
    it("seeded with resumeConversationId, the FIRST turn's request body carries that conversation_id (decision 4)", async () => {
      sendTurnMock.mockResolvedValueOnce({
        ok: true,
        turn: {
          conversationId: "conv-resumed",
          messageId: "msg-1",
          reply: "Continuing our chat.",
          decision: "answer",
          confidence: 0.9,
          sources: [],
          action: null,
        },
      });

      act(() => {
        root.render(
          <ChatWidget config={baseConfig} expiresAt="2026-07-16T12:30:00Z" resumeConversationId="conv-resumed" />,
        );
      });
      openPanel();

      typeAndSend("Are you still there?");
      await flush();

      expect(sendTurnMock).toHaveBeenCalledWith(
        baseConfig,
        expect.objectContaining({ message: "Are you still there?", conversationId: "conv-resumed" }),
      );
    });

    it("after a successful turn, touchResumeRecord is called with the returned conversation_id, but ONLY when resume_enabled", async () => {
      isResumeEnabledMock.mockReturnValue(true);
      sendTurnMock.mockResolvedValueOnce({
        ok: true,
        turn: {
          conversationId: "conv-resumed",
          messageId: "msg-1",
          reply: "Continuing our chat.",
          decision: "answer",
          confidence: 0.9,
          sources: [],
          action: null,
        },
      });

      act(() => {
        root.render(
          <ChatWidget config={baseConfig} expiresAt="2026-07-16T12:30:00Z" resumeConversationId="conv-resumed" />,
        );
      });
      openPanel();

      typeAndSend("Are you still there?");
      await flush();

      expect(touchResumeRecordMock).toHaveBeenCalledWith("conv-resumed", expect.any(Date));
    });

    it("resume_enabled false -> a successful turn does NOT call touchResumeRecord", async () => {
      isResumeEnabledMock.mockReturnValue(false);
      sendTurnMock.mockResolvedValueOnce({
        ok: true,
        turn: {
          conversationId: "conv-1",
          messageId: "msg-1",
          reply: "Hi!",
          decision: "answer",
          confidence: 0.9,
          sources: [],
          action: null,
        },
      });

      act(() => {
        root.render(<ChatWidget config={baseConfig} expiresAt="2026-07-16T12:30:00Z" />);
      });
      openPanel();

      typeAndSend("Hello");
      await flush();

      expect(touchResumeRecordMock).not.toHaveBeenCalled();
    });

    it("RESUME_REJECTED (decision 7 case c): a first post-resume turn returning CONVERSATION_NOT_FOUND clears the stored record, silently retries with conversation_id:null, adopts the backend's fresh conversation_id, renders the REAL reply in a new thread, and shows NO error bubble and NO fabricated prior messages", async () => {
      const notFound: TurnResult = {
        ok: false,
        error: {
          type: "TURN_ERROR",
          errorCode: "CONVERSATION_NOT_FOUND",
          message: "Conversation not found.",
          correlationId: "corr-nf-1",
          status: 404,
          retryAfterSeconds: null,
        },
      };
      sendTurnMock.mockResolvedValueOnce(notFound);
      sendTurnMock.mockResolvedValueOnce({
        ok: true,
        turn: {
          conversationId: "conv-fresh",
          messageId: "msg-fresh",
          reply: "Hi! Starting fresh.",
          decision: "answer",
          confidence: 0.9,
          sources: [],
          action: null,
        },
      });
      const consoleErrorSpy = vi.spyOn(console, "error").mockImplementation(() => {});

      act(() => {
        root.render(
          <ChatWidget config={baseConfig} expiresAt="2026-07-16T12:30:00Z" resumeConversationId="conv-stale" />,
        );
      });
      openPanel();

      typeAndSend("Hello again");
      await flush();
      await flush();

      // The stale/foreign record was cleared (decision 7).
      expect(clearResumeRecordMock).toHaveBeenCalledTimes(1);

      // Retried with conversation_id: null (silently), landing the real reply.
      expect(sendTurnMock).toHaveBeenCalledTimes(2);
      expect(sendTurnMock).toHaveBeenNthCalledWith(
        1,
        baseConfig,
        expect.objectContaining({ conversationId: "conv-stale" }),
      );
      expect(sendTurnMock).toHaveBeenNthCalledWith(
        2,
        baseConfig,
        expect.objectContaining({ conversationId: null }),
      );

      // The REAL reply rendered, in the backend's freshly-created thread.
      const botBubbles = container.querySelectorAll(".cw-bubble-row-bot .cw-bubble-bot");
      expect(botBubbles.length).toBe(1);
      expect(botBubbles[0]?.textContent).toContain("Hi! Starting fresh.");

      // NO error bubble, NO fabricated prior messages.
      expect(container.querySelector(".cw-line-error")).toBeNull();
      expect(container.querySelectorAll(".cw-bubble-row-user").length).toBe(1); // only the one real user message

      // A SECOND turn continues the NEW thread (conv-fresh), never the stale one.
      sendTurnMock.mockResolvedValueOnce({
        ok: true,
        turn: {
          conversationId: "conv-fresh",
          messageId: "msg-fresh-2",
          reply: "Sure thing.",
          decision: "answer",
          confidence: 0.9,
          sources: [],
          action: null,
        },
      });
      typeAndSend("Follow-up");
      await flush();

      expect(sendTurnMock).toHaveBeenLastCalledWith(
        baseConfig,
        expect.objectContaining({ conversationId: "conv-fresh" }),
      );

      expect(consoleErrorSpy).toHaveBeenCalledWith(expect.stringContaining("RESUME_REJECTED"));
    });

    it("a CONVERSATION_NOT_FOUND with NO resumed id in play keeps the S14.2 honest-error behavior (regression)", async () => {
      const notFound: TurnResult = {
        ok: false,
        error: {
          type: "TURN_ERROR",
          errorCode: "CONVERSATION_NOT_FOUND",
          message: "Conversation not found.",
          correlationId: "corr-nf-2",
          status: 404,
          retryAfterSeconds: null,
        },
      };
      sendTurnMock.mockResolvedValueOnce(notFound);
      vi.spyOn(console, "error").mockImplementation(() => {});

      act(() => {
        // NO resumeConversationId prop -- a normal S14.1/S14.2 boot.
        root.render(<ChatWidget config={baseConfig} expiresAt="2026-07-16T12:30:00Z" />);
      });
      openPanel();

      typeAndSend("Hello");
      await flush();

      // Only ONE attempt -- no silent retry-as-new-conversation happens
      // when there was no resume in play.
      expect(sendTurnMock).toHaveBeenCalledTimes(1);
      expect(clearResumeRecordMock).not.toHaveBeenCalled();

      const errorLine = container.querySelector(".cw-line-error");
      expect(errorLine).not.toBeNull();
      expect(errorLine?.textContent).toMatch(/something went wrong/i);
      expect(container.querySelectorAll(".cw-bubble-row-bot .cw-bubble-bot").length).toBe(0);
    });
  });

  describe("booking chip routes directly to scheduling (bypasses the orchestrator escalate copy)", () => {
    it("clicking the 'Book a call with sales' suggestion chip calls fetchAvailabilitySummary (NOT sendTurn) and renders the scheduling card, not a handoff/support interstitial", async () => {
      fetchAvailabilitySummaryMock.mockResolvedValueOnce({
        ok: true,
        summary: {
          action: "schedule_cta",
          timezone: "UTC",
          days: [{ date: "2026-07-22", hasAvailability: true }],
          transitionMessage: "Happy to connect you with a sales rep. Please pick a time that works best for you.",
          existingBooking: null,
        },
      });

      act(() => {
        root.render(<ChatWidget config={baseConfig} expiresAt="2026-07-16T12:30:00Z" />);
      });
      openPanel();

      const suggestion = Array.from(container.querySelectorAll<HTMLButtonElement>(".cw-suggestion")).find(
        (button) => button.textContent?.includes("Book a call with sales"),
      );
      expect(suggestion).toBeDefined();

      act(() => {
        suggestion?.click();
      });
      await flush();

      // Went through the same explicit-booking path as the persistent CTA
      // button -- no /public/chat/message turn, no classify/generate/cost.
      expect(fetchAvailabilitySummaryMock).toHaveBeenCalledTimes(1);
      expect(sendTurnMock).not.toHaveBeenCalled();

      expect(container.querySelector(".cw-bubble-row-user")?.textContent).toBe("Book a call with sales");
      // Straight to the calendar card -- never the handoff-choice interstitial.
      expect(container.querySelector(".cw-handoff-card")).toBeNull();
      const botBubbles = container.querySelectorAll(".cw-bubble-row-bot .cw-bubble-bot");
      expect(botBubbles.length).toBe(1);
      expect(container.querySelector(".cw-bubble-row-bot .cw-sched-day-strip")).not.toBeNull();
    });

    it("every other suggestion chip still goes through sendMessage/the orchestrator turn unchanged", async () => {
      sendTurnMock.mockResolvedValueOnce({
        ok: true,
        turn: {
          conversationId: "conv-support",
          messageId: "msg-support",
          reply: "Sure, tell me more about the problem.",
          decision: "answer",
          confidence: 0.9,
          sources: [],
          action: null,
        },
      });

      act(() => {
        root.render(<ChatWidget config={baseConfig} expiresAt="2026-07-16T12:30:00Z" />);
      });
      openPanel();

      const suggestion = Array.from(container.querySelectorAll<HTMLButtonElement>(".cw-suggestion")).find(
        (button) => button.textContent?.includes("I have a roof leak, can you help?"),
      );
      act(() => {
        suggestion?.click();
      });
      await flush();

      expect(sendTurnMock).toHaveBeenCalledWith(
        baseConfig,
        expect.objectContaining({ message: "I have a roof leak, can you help?", conversationId: null }),
      );
      expect(fetchAvailabilitySummaryMock).not.toHaveBeenCalled();
    });
  });

  describe("orchestrator escalate turns render the server's real reply text", () => {
    it("a bot-initiated escalate (e.g. low confidence) renders the server's actual reply, not a hardcoded client-side apology string, and still shows the handoff-choice interstitial", async () => {
      sendTurnMock.mockResolvedValueOnce({
        ok: true,
        turn: {
          conversationId: "conv-1",
          messageId: "msg-1",
          reply: "I couldn't find a confident answer to that -- let's get you to someone who can help.",
          decision: "escalate",
          confidence: 0.1,
          sources: [],
          action: "lead_form",
        },
      });

      act(() => {
        root.render(<ChatWidget config={baseConfig} expiresAt="2026-07-16T12:30:00Z" />);
      });
      openPanel();

      typeAndSend("Some off-topic or low-confidence question");
      await flush();

      const botBubble = container.querySelector(".cw-bubble-row-bot .cw-bubble-bot");
      expect(botBubble?.textContent).toContain(
        "I couldn't find a confident answer to that -- let's get you to someone who can help.",
      );
      expect(botBubble?.textContent).not.toMatch(/sorry to hear/i);
      // Still a genuine bot-initiated escalate -- the one-step confirm
      // interstitial still renders, just with accurate reply text above it.
      expect(container.querySelector(".cw-handoff-card")).not.toBeNull();
    });
  });

  describe("SR-5: persistent 'Connect with a sales rep' CTA", () => {
    function getConnectButton(): HTMLButtonElement {
      const button = container.querySelector<HTMLButtonElement>(".cw-connect-sales-button");
      if (!button) throw new Error("connect-sales button not found");
      return button;
    }

    it("the persistent CTA is visible above the input before any conversation", () => {
      act(() => {
        root.render(<ChatWidget config={baseConfig} expiresAt="2026-07-16T12:30:00Z" />);
      });
      openPanel();

      expect(container.querySelector(".cw-connect-sales-button")).not.toBeNull();
      expect(container.querySelectorAll(".cw-bubble-row").length).toBe(0);
    });

    it("clicking the CTA calls fetchAvailabilitySummary (NOT sendTurn) and renders a user bubble + the fixed transition bot bubble + the in-thread staged picker", async () => {
      fetchAvailabilitySummaryMock.mockResolvedValueOnce({
        ok: true,
        summary: {
          action: "schedule_cta",
          timezone: "UTC",
          days: [{ date: "2026-07-22", hasAvailability: true }],
          transitionMessage: "I'd be happy to help you find a time with our sales team.",
          existingBooking: null,
        },
      });
      fetchSlotsMock.mockResolvedValueOnce({ ok: true, slots: [] });

      act(() => {
        root.render(<ChatWidget config={baseConfig} expiresAt="2026-07-16T12:30:00Z" />);
      });
      openPanel();

      act(() => {
        getConnectButton().click();
      });
      await flush();

      expect(fetchAvailabilitySummaryMock).toHaveBeenCalledTimes(1);
      expect(sendTurnMock).not.toHaveBeenCalled();

      expect(container.querySelector(".cw-bubble-row-user")?.textContent).toBe("Connect with a sales rep");
      const botBubbles = container.querySelectorAll(".cw-bubble-row-bot .cw-bubble-bot");
      expect(botBubbles.length).toBe(1);
      expect(botBubbles[0]?.textContent).toContain("I'd be happy to help you find a time with our sales team.");
      // The staged date strip renders in the bot turn, inside the message thread.
      expect(container.querySelector(".cw-bubble-row-bot .cw-sched-day-strip")).not.toBeNull();
    });

    it("action=lead_form renders the fixed transition bubble + the lead form, not the picker", async () => {
      fetchAvailabilitySummaryMock.mockResolvedValueOnce({
        ok: true,
        summary: {
          action: "lead_form",
          timezone: "UTC",
          days: [],
          transitionMessage: "I'd be happy to help you find a time with our sales team.",
          existingBooking: null,
        },
      });

      act(() => {
        root.render(<ChatWidget config={baseConfig} expiresAt="2026-07-16T12:30:00Z" />);
      });
      openPanel();

      act(() => {
        getConnectButton().click();
      });
      await flush();

      expect(container.querySelector("form.cw-lead-form")).not.toBeNull();
      expect(container.querySelector(".cw-sched-calendar")).toBeNull();
    });

    it("existingBooking non-null shows the keep-vs-book-another ask before the picker", async () => {
      fetchAvailabilitySummaryMock.mockResolvedValueOnce({
        ok: true,
        summary: {
          action: "schedule_cta",
          timezone: "UTC",
          days: [{ date: "2026-07-22", hasAvailability: true }],
          transitionMessage: "I'd be happy to help you find a time with our sales team.",
          existingBooking: { startsAt: "2026-07-22T09:00:00+00:00", endsAt: "2026-07-22T09:30:00+00:00", timezone: "UTC" },
        },
      });

      act(() => {
        root.render(<ChatWidget config={baseConfig} expiresAt="2026-07-16T12:30:00Z" />);
      });
      openPanel();

      act(() => {
        getConnectButton().click();
      });
      await flush();

      expect(container.querySelector(".cw-sched-calendar")).toBeNull();
      const askText = container.querySelector(".cw-bubble-row-bot .cw-sched")?.textContent ?? "";
      expect(askText).toMatch(/already booked/i);
      expect(askText).toMatch(/keep it/i);
    });

    it("a fetchAvailabilitySummary failure shows an honest error with manual retry, never a fabricated picker", async () => {
      fetchAvailabilitySummaryMock.mockResolvedValueOnce({
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
        root.render(<ChatWidget config={baseConfig} expiresAt="2026-07-16T12:30:00Z" />);
      });
      openPanel();

      act(() => {
        getConnectButton().click();
      });
      await flush();

      expect(container.querySelector(".cw-sched-calendar")).toBeNull();
      expect(container.querySelectorAll(".cw-bubble-row-bot .cw-bubble-bot").length).toBe(0);
      const errorLine = container.querySelector(".cw-sched-error");
      expect(errorLine).not.toBeNull();
      expect(errorLine?.querySelector(".cw-sched-retry")).not.toBeNull();
      expect(consoleErrorSpy).toHaveBeenCalledWith(expect.stringContaining("NETWORK_ERROR"));
    });

    it("a 401 on the CTA click triggers the bounded session-reconnect + retry (mirrors runSend's decision 5), succeeding on a valid re-mint", async () => {
      fetchAvailabilitySummaryMock.mockResolvedValueOnce({
        ok: false,
        error: {
          type: "SCHEDULE_ERROR",
          errorCode: "AUTHENTICATION_ERROR",
          message: "Token has expired.",
          correlationId: "corr-401",
          status: 401,
          retryAfterSeconds: null,
        },
      });
      mintVisitorSessionMock.mockResolvedValueOnce({
        ok: true,
        session: { visitorToken: "jwt.fresh", expiresAt: "2026-07-16T13:00:00Z" },
      });
      fetchAvailabilitySummaryMock.mockResolvedValueOnce({
        ok: true,
        summary: {
          action: "schedule_cta",
          timezone: "UTC",
          days: [{ date: "2026-07-22", hasAvailability: true }],
          transitionMessage: "I'd be happy to help you find a time with our sales team.",
          existingBooking: null,
        },
      });
      fetchSlotsMock.mockResolvedValueOnce({ ok: true, slots: [] });
      vi.spyOn(console, "error").mockImplementation(() => {});

      act(() => {
        root.render(<ChatWidget config={baseConfig} expiresAt="2026-07-16T12:30:00Z" />);
      });
      openPanel();

      act(() => {
        getConnectButton().click();
      });
      await flush();
      await flush();

      expect(mintVisitorSessionMock).toHaveBeenCalledTimes(1);
      expect(fetchAvailabilitySummaryMock).toHaveBeenCalledTimes(2);
      const botBubbles = container.querySelectorAll(".cw-bubble-row-bot .cw-bubble-bot");
      expect(botBubbles.length).toBe(1);
      expect(botBubbles[0]?.textContent).toContain("I'd be happy to help you find a time with our sales team.");
      expect(container.querySelector(".cw-sched-error")).toBeNull();
    });

    it("a 401 on the CTA click that fails to reconnect shows an honest error, never a fabricated picker", async () => {
      fetchAvailabilitySummaryMock.mockResolvedValueOnce({
        ok: false,
        error: {
          type: "SCHEDULE_ERROR",
          errorCode: "AUTHENTICATION_ERROR",
          message: "Token has expired.",
          correlationId: "corr-401",
          status: 401,
          retryAfterSeconds: null,
        },
      });
      mintVisitorSessionMock.mockResolvedValue({
        ok: false,
        error: {
          type: "ADMISSION_ERROR",
          errorCode: "NETWORK_ERROR",
          message: "Network request failed.",
          correlationId: null,
          status: null,
          retryAfterSeconds: null,
        },
      });
      vi.spyOn(console, "error").mockImplementation(() => {});

      act(() => {
        root.render(<ChatWidget config={baseConfig} expiresAt="2026-07-16T12:30:00Z" />);
      });
      openPanel();

      act(() => {
        getConnectButton().click();
      });
      await flush();
      await flush();

      expect(mintVisitorSessionMock.mock.calls.length).toBeGreaterThan(0);
      expect(container.querySelectorAll(".cw-bubble-row-bot .cw-bubble-bot").length).toBe(0);
      expect(container.querySelector(".cw-sched-error")).not.toBeNull();
    });

    // SR-6: Calendly hosted-handoff flow.
    describe("action=calendly_handoff (SR-6)", () => {
      const calendlySummary = {
        action: "calendly_handoff" as const,
        timezone: "UTC",
        days: [],
        transitionMessage: "I'd be happy to help you find a time with our sales team.",
        existingBooking: null,
        schedulingUrl: "https://calendly.com/acme/intro",
      };

      it("renders the fixed transition message + the email step, NOT the native ScheduleCta picker", async () => {
        fetchAvailabilitySummaryMock.mockResolvedValueOnce({ ok: true, summary: calendlySummary });

        act(() => {
          root.render(<ChatWidget config={baseConfig} expiresAt="2026-07-16T12:30:00Z" />);
        });
        openPanel();
        act(() => {
          getConnectButton().click();
        });
        await flush();

        const botBubbles = container.querySelectorAll(".cw-bubble-row-bot .cw-bubble-bot");
        expect(botBubbles.length).toBe(1);
        expect(botBubbles[0]?.textContent).toContain("I'd be happy to help you find a time with our sales team.");
        expect(container.querySelector(".cw-sched-calendar")).toBeNull();
        expect(container.querySelector("#cw-sched-handoff-email")).not.toBeNull();
      });

      it("submitting the email calls postHandoffIntent, then reveals the link-out button", async () => {
        fetchAvailabilitySummaryMock.mockResolvedValueOnce({ ok: true, summary: calendlySummary });

        act(() => {
          root.render(<ChatWidget config={baseConfig} expiresAt="2026-07-16T12:30:00Z" />);
        });
        openPanel();
        act(() => {
          getConnectButton().click();
        });
        await flush();

        const emailInput = container.querySelector<HTMLInputElement>("#cw-sched-handoff-email");
        if (!emailInput) throw new Error("email input not found");
        act(() => {
          setNativeInputValue(emailInput, "visitor@example.com");
          emailInput.dispatchEvent(new Event("input", { bubbles: true }));
        });
        const nameInput = container.querySelector<HTMLInputElement>("#cw-sched-handoff-name");
        if (!nameInput) throw new Error("name input not found");
        act(() => {
          setNativeInputValue(nameInput, "Visitor Name");
          nameInput.dispatchEvent(new Event("input", { bubbles: true }));
        });

        const continueButton = container.querySelector<HTMLButtonElement>(".cw-sched-handoff-continue-button");
        if (!continueButton) throw new Error("continue button not found");
        act(() => {
          continueButton.click();
        });
        await flush();

        expect(postHandoffIntentMock).toHaveBeenCalledTimes(1);
        expect(postHandoffIntentMock).toHaveBeenCalledWith(baseConfig, { email: "visitor@example.com" });

        const linkButton = container.querySelector<HTMLButtonElement>(".cw-sched-handoff-link-button");
        expect(linkButton).not.toBeNull();
      });

      it("clicking the link-out button calls window.open(schedulingUrl, '_blank', 'noopener,noreferrer') and never injects a script/iframe", async () => {
        fetchAvailabilitySummaryMock.mockResolvedValueOnce({ ok: true, summary: calendlySummary });
        const windowOpenSpy = vi.spyOn(window, "open").mockImplementation(() => null);

        act(() => {
          root.render(<ChatWidget config={baseConfig} expiresAt="2026-07-16T12:30:00Z" />);
        });
        openPanel();
        act(() => {
          getConnectButton().click();
        });
        await flush();

        const emailInput = container.querySelector<HTMLInputElement>("#cw-sched-handoff-email");
        if (!emailInput) throw new Error("email input not found");
        act(() => {
          setNativeInputValue(emailInput, "visitor@example.com");
          emailInput.dispatchEvent(new Event("input", { bubbles: true }));
        });
        const nameInput = container.querySelector<HTMLInputElement>("#cw-sched-handoff-name");
        if (!nameInput) throw new Error("name input not found");
        act(() => {
          setNativeInputValue(nameInput, "Visitor Name");
          nameInput.dispatchEvent(new Event("input", { bubbles: true }));
        });
        act(() => {
          container.querySelector<HTMLButtonElement>(".cw-sched-handoff-continue-button")?.click();
        });
        await flush();

        const linkButton = container.querySelector<HTMLButtonElement>(".cw-sched-handoff-link-button");
        if (!linkButton) throw new Error("link-out button not found");
        act(() => {
          linkButton.click();
        });

        expect(windowOpenSpy).toHaveBeenCalledTimes(1);
        expect(windowOpenSpy).toHaveBeenCalledWith(
          "https://calendly.com/acme/intro",
          "_blank",
          "noopener,noreferrer",
        );
        expect(container.querySelector("script[src*='calendly']")).toBeNull();
        expect(container.querySelector("iframe")).toBeNull();
      });

      it("a postHandoffIntent failure shows an honest error + retry and does NOT reveal the link-out button", async () => {
        fetchAvailabilitySummaryMock.mockResolvedValueOnce({ ok: true, summary: calendlySummary });
        postHandoffIntentMock.mockResolvedValueOnce({
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
        const windowOpenSpy = vi.spyOn(window, "open").mockImplementation(() => null);

        act(() => {
          root.render(<ChatWidget config={baseConfig} expiresAt="2026-07-16T12:30:00Z" />);
        });
        openPanel();
        act(() => {
          getConnectButton().click();
        });
        await flush();

        const emailInput = container.querySelector<HTMLInputElement>("#cw-sched-handoff-email");
        if (!emailInput) throw new Error("email input not found");
        act(() => {
          setNativeInputValue(emailInput, "visitor@example.com");
          emailInput.dispatchEvent(new Event("input", { bubbles: true }));
        });
        const nameInput = container.querySelector<HTMLInputElement>("#cw-sched-handoff-name");
        if (!nameInput) throw new Error("name input not found");
        act(() => {
          setNativeInputValue(nameInput, "Visitor Name");
          nameInput.dispatchEvent(new Event("input", { bubbles: true }));
        });
        act(() => {
          container.querySelector<HTMLButtonElement>(".cw-sched-handoff-continue-button")?.click();
        });
        await flush();

        expect(container.querySelector(".cw-sched-handoff-link-button")).toBeNull();
        expect(container.querySelector(".cw-sched-error")).not.toBeNull();
        expect(windowOpenSpy).not.toHaveBeenCalled();
        expect(consoleErrorSpy).toHaveBeenCalledWith(expect.stringContaining("NETWORK_ERROR"));
      });

      it("the link-out button is a focusable <button> with an accessible 'opens in a new tab' label", async () => {
        fetchAvailabilitySummaryMock.mockResolvedValueOnce({ ok: true, summary: calendlySummary });

        act(() => {
          root.render(<ChatWidget config={baseConfig} expiresAt="2026-07-16T12:30:00Z" />);
        });
        openPanel();
        act(() => {
          getConnectButton().click();
        });
        await flush();

        const emailInput = container.querySelector<HTMLInputElement>("#cw-sched-handoff-email");
        if (!emailInput) throw new Error("email input not found");
        act(() => {
          setNativeInputValue(emailInput, "visitor@example.com");
          emailInput.dispatchEvent(new Event("input", { bubbles: true }));
        });
        const nameInput = container.querySelector<HTMLInputElement>("#cw-sched-handoff-name");
        if (!nameInput) throw new Error("name input not found");
        act(() => {
          setNativeInputValue(nameInput, "Visitor Name");
          nameInput.dispatchEvent(new Event("input", { bubbles: true }));
        });
        act(() => {
          container.querySelector<HTMLButtonElement>(".cw-sched-handoff-continue-button")?.click();
        });
        await flush();

        const linkButton = container.querySelector<HTMLButtonElement>(".cw-sched-handoff-link-button");
        expect(linkButton?.tagName).toBe("BUTTON");
        expect(linkButton?.getAttribute("aria-label")?.toLowerCase()).toContain("new tab");
      });
    });
  });
});

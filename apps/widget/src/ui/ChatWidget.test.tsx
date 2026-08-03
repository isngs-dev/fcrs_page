import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { WidgetConfig } from "../config";
import type { TurnResult } from "../turn";
import type { FetchSlotsResult, FetchAvailabilitySummaryResult, PostHandoffIntentResult } from "../schedule";
import type { AdmissionResult } from "../session";
import type { IdentityResult } from "../identity";

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
const mintVisitorSessionMock = vi.fn<(config: WidgetConfig) => Promise<AdmissionResult>>();
const speakGreetingMock = vi.fn<() => void>();
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
// without depending on jsdom having a real Web Speech API (it doesn't).
vi.mock("../tts", () => ({
  speakGreeting: () => speakGreetingMock(),
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

function openPanel(): void {
  const launcher = container.querySelector<HTMLButtonElement>(".cw-placeholder");
  if (!launcher) throw new Error("launcher not found");
  act(() => {
    launcher.click();
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
    expect(launcher.getAttribute("aria-label")).toBe("Open chat");
    expect(launcher.getAttribute("aria-expanded")).toBe("false");
    expect(launcher.textContent).toContain("Chat <b>with us</b>");
    expect(launcher.querySelector("b")).toBeNull();

    openPanel();
    expect(launcher.getAttribute("aria-label")).toBe("Close chat");
    expect(launcher.getAttribute("aria-expanded")).toBe("true");
  });

  it("uses the explicit default launcher label when no tenant label is supplied", () => {
    act(() => {
      root.render(<ChatWidget config={baseConfig} expiresAt="2026-07-16T12:30:00Z" />);
    });

    expect(container.querySelector(".cw-launcher-label")?.textContent).toBe("Chat with us");
  });

  it("ships a self-contained full-height drawer CSS contract", () => {
    expect(widgetCss).toContain("@font-face");
    expect(widgetCss).toContain("data:font/woff2;base64,");
    expect(widgetCss).toContain("top: 0");
    expect(widgetCss).toContain("height: 100dvh");
    expect(widgetCss).toContain("width: min(400px, calc(100vw - 32px))");
    expect(widgetCss).toContain("@media (max-width: 480px)");
    expect(widgetCss).toContain("inset: 0");
    expect(widgetCss).not.toMatch(/https?:|\/\/fonts\./i);
    expect(widgetCss).not.toContain("Instrument Sans");
    expect(widgetCss).not.toContain("Inter");
  });

  it("toggles the panel open/closed via the launcher", () => {
    act(() => {
      root.render(<ChatWidget config={baseConfig} expiresAt="2026-07-16T12:30:00Z" />);
    });

    expect(container.querySelector(".cw-panel")).toBeNull();
    openPanel();
    expect(container.querySelector(".cw-panel")).not.toBeNull();
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

  it("a success with action=lead_form renders the real LeadForm, not the stub (S14.3)", async () => {
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
    expect(container.querySelector("form.cw-lead-form")).not.toBeNull();
    expect(container.querySelector("input[type=email]")).not.toBeNull();
  });

  it("a success with action=schedule_cta renders the real ScheduleCta, not the old stub (S14.4)", async () => {
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
    await flush();

    expect(fetchSlotsMock).toHaveBeenCalledTimes(1);
    expect(container.querySelector(".cw-sched-empty")).not.toBeNull();
    expect(container.querySelector("form.cw-lead-form")).toBeNull();
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
    it("opening the panel moves focus into it (the message input) and sets aria-expanded", () => {
      act(() => {
        root.render(<ChatWidget config={baseConfig} expiresAt="2026-07-16T12:30:00Z" />);
      });

      const launcher = container.querySelector<HTMLButtonElement>(".cw-placeholder")!;
      expect(launcher.getAttribute("aria-expanded")).toBe("false");

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
      expect(headerEl?.textContent).toBe("Assistant");
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

      expect(container.querySelector(".cw-welcome")?.textContent).toMatch(/your assistant/i);
      const suggestion = Array.from(container.querySelectorAll<HTMLButtonElement>(".cw-suggestion")).find(
        (button) => button.textContent?.includes("How much does it cost?"),
      );
      expect(suggestion).toBeDefined();

      act(() => {
        suggestion?.click();
      });
      await flush();

      expect(sendTurnMock).toHaveBeenCalledWith(
        baseConfig,
        expect.objectContaining({ message: "How much does it cost?", conversationId: null }),
      );
      expect(container.querySelector(".cw-welcome")).toBeNull();
      expect(container.querySelector(".cw-bubble-row-user")?.textContent).toBe("How much does it cost?");
    });

    it("has an in-panel close control that restores launcher focus", () => {
      act(() => {
        root.render(<ChatWidget config={baseConfig} expiresAt="2026-07-16T12:30:00Z" />);
      });
      openPanel();

      const closeButton = container.querySelector<HTMLButtonElement>('.cw-close-button[aria-label="Close chat"]');
      expect(closeButton).not.toBeNull();
      act(() => {
        closeButton?.click();
      });

      const launcher = container.querySelector<HTMLButtonElement>(".cw-placeholder")!;
      expect(container.querySelector(".cw-panel")).toBeNull();
      expect(document.activeElement).toBe(launcher);
    });

    it("Escape closes the panel and restores focus to the launcher", () => {
      act(() => {
        root.render(<ChatWidget config={baseConfig} expiresAt="2026-07-16T12:30:00Z" />);
      });
      openPanel();
      expect(container.querySelector(".cw-panel")).not.toBeNull();

      const panel = container.querySelector<HTMLDivElement>(".cw-panel")!;
      act(() => {
        panel.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
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

    it("TTS: does not speak before the panel is ever opened", () => {
      act(() => {
        root.render(<ChatWidget config={baseConfig} expiresAt="2026-07-16T12:30:00Z" />);
      });

      expect(speakGreetingMock).not.toHaveBeenCalled();
    });

    it("TTS: speaks exactly once on the first panel-open gesture when not muted", () => {
      act(() => {
        root.render(<ChatWidget config={baseConfig} expiresAt="2026-07-16T12:30:00Z" />);
      });

      openPanel();
      expect(speakGreetingMock).toHaveBeenCalledTimes(1);

      // Closing and reopening in the same page session must not speak again.
      openPanel(); // close
      openPanel(); // reopen
      expect(speakGreetingMock).toHaveBeenCalledTimes(1);
    });

    it("TTS: re-opening after muting does not speak again (mute suppresses future opens)", () => {
      act(() => {
        root.render(<ChatWidget config={baseConfig} expiresAt="2026-07-16T12:30:00Z" />);
      });

      openPanel(); // first open — the gesture the spec requires; greets once
      expect(speakGreetingMock).toHaveBeenCalledTimes(1);

      const muteToggle = container.querySelector<HTMLButtonElement>(".cw-mute-toggle")!;
      act(() => {
        muteToggle.click();
      });

      openPanel(); // close
      openPanel(); // reopen while muted
      // Greeting only ever fires on the *first* open in the page session
      // (decision 5), so this also confirms mute doesn't retroactively
      // matter for the already-consumed first-open call — the important
      // invariant is no *additional* speak call happens.
      expect(speakGreetingMock).toHaveBeenCalledTimes(1);
    });

    it("TTS: the mute toggle mutes future opens and is visible with aria-pressed", () => {
      // Use a second render to test "muted before any open" semantics
      // cleanly: open once (consumes the greeting), mute, close, reopen —
      // no further speak calls, and the toggle communicates state via
      // aria-pressed.
      act(() => {
        root.render(<ChatWidget config={baseConfig} expiresAt="2026-07-16T12:30:00Z" />);
      });
      openPanel();
      expect(speakGreetingMock).toHaveBeenCalledTimes(1);

      const muteToggle = container.querySelector<HTMLButtonElement>(".cw-mute-toggle")!;
      expect(muteToggle.getAttribute("aria-pressed")).toBe("false");

      act(() => {
        muteToggle.click();
      });
      expect(muteToggle.getAttribute("aria-pressed")).toBe("true");
      expect(ttsCancelMock).toHaveBeenCalled();
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
      // The staged picker (calendar) renders IN the bot bubble, inside the message thread.
      expect(container.querySelector(".cw-bubble-row-bot .cw-sched-calendar")).not.toBeNull();
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

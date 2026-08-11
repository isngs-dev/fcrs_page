/**
 * Top-level chat component (S14.2 decision 1, scope item 2).
 *
 * Rendered by `entry.tsx` in place of S14.1's `<Placeholder>` on admission
 * success. Owns open/closed state, the in-memory message list, and the
 * in-memory `conversation_id` (decision 4). Renders the launcher
 * (open/close toggle) and, when open, the panel (header, message list,
 * input row). Orchestrates a send: optimistic user bubble -> typing
 * indicator -> sendTurn -> bot bubble (or error line, decision 7) -> store
 * the returned conversation_id.
 *
 * S14.5 adds: panel focus management (focus-in on open, a hand-rolled focus
 * trap while open, Escape-to-close, focus-restore to the launcher on close
 * — decision 1), `aria-labelledby` tying the dialog to its header, and the
 * opt-in TTS greeting + mute toggle (decision 5) triggered by the first
 * open gesture in this page session.
 *
 * S14.6 adds (decisions 1-7, scope item 3): a turn send is wrapped in
 * `withRetry` so a transient network/5xx/429 failure gets a **bounded**
 * auto-retry with a visible "retrying" status before an honest "offline" +
 * manual Retry (never a fabricated reply); a `401`/`403` triggers a
 * **bounded** single re-mint attempt (`mintVisitorSession`) rather than an
 * unbounded loop against the rate-limited admission endpoint; the
 * connection-status indicator (`ConnectionStatus`) renders in the header and
 * announces state *transitions* politely (not every retry tick); all
 * retry/reconnect timers are guarded by an `unmounted` ref so no fetch fires
 * after the panel/component is gone (the zombie-retry guard, decision 7).
 *
 * SR-3 adds (decisions 4/7/8, scope item 4): an optional `resumeConversationId`
 * prop seeds `conversationIdRef` instead of always starting `null` (decision
 * 4 -- the first turn after a resume carries the SAME conversation_id, so
 * the thread continues server-side). After each successful turn,
 * `touchResumeRecord` refreshes the stored record's `lastActive`/
 * `conversationId` -- but ONLY when `session.ts#isResumeEnabled()` says the
 * tenant opted in (no writes at all when off, decision 8). If the FIRST turn
 * after a resume returns `CONVERSATION_NOT_FOUND` (the stored id was stale/
 * foreign/rejected -- decision 7's isolation-safety path made visible), the
 * widget clears the stored record, adopts the backend's freshly-created
 * conversation_id from that same response, and continues with the real
 * reply in a new thread -- no error bubble, no fabricated history. A
 * `CONVERSATION_NOT_FOUND` with no resumed id in play keeps S14.2's honest
 * error line unchanged (this is a NEW branch, not a replacement).
 */
import { useCallback, useEffect, useRef, useState } from "react";

import type { WidgetConfig } from "../config";
import { sendTurn, type TurnResult } from "../turn";
import { clearResumeRecord, touchResumeRecord } from "../resume";
import { isResumeEnabled, mintVisitorSession } from "../session";
import { withRetry } from "../retry";
import { fetchAvailabilitySummary } from "../schedule";
import * as tts from "../tts";
import type { ChatMessage } from "./Bubble";
import { MessageList } from "./MessageList";
import { ConnectionStatus, type ConnectionState } from "./ConnectionStatus";

const LOG_PREFIX = "[chatbot-widget]";
const PANEL_HEADER_ID = "cw-panel-header";
const SUPPORT_HANDOFF_REPLY = "I'm sorry to hear that — let's get it sorted. I can connect you with one of our reps.";
const SUPPORT_STAY_REPLY = "No problem, I'm right here. Can you tell me a bit more about what stopped working?";

/** Max attempts for the bounded auto-retry of a transient turn failure (decision 1/2). */
const TURN_RETRY_MAX_ATTEMPTS = 4;
/** Max attempts for the bounded expired-session re-mint (decision 5) — small
 * and capped since the re-mint itself hits the rate-limited /widget/session
 * endpoint; an unbounded loop here would storm it. */
const REMINT_MAX_ATTEMPTS = 2;

/** Small inline SVGs keep the embed self-contained without adding an icon package. */
function ChatGlyph({ name }: { name: "chat" | "close" | "sound" | "muted" | "send" | "reset" | "mic" }) {
  const common = { width: 20, height: 20, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 1.9 };
  if (name === "close") {
    return <svg aria-hidden="true" {...common}><path d="m6 6 12 12M18 6 6 18" /></svg>;
  }
  if (name === "send") {
    return <svg aria-hidden="true" {...common}><path d="M5 12h14M13 6l6 6-6 6" /></svg>;
  }
  if (name === "reset") {
    return <svg aria-hidden="true" {...common}><path d="M3 12a9 9 0 1 0 3-6.7L3 8" /><path d="M3 3v5h5" /></svg>;
  }
  if (name === "mic") {
    return <svg aria-hidden="true" {...common}><path d="M12 2a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z" /><path d="M19 10v1a7 7 0 0 1-14 0v-1M12 18v4" /></svg>;
  }
  if (name === "sound" || name === "muted") {
    return (
      <svg aria-hidden="true" {...common}>
        <path d="M11 5 6 9H3v6h3l5 4Z" />
        {name === "sound" ? <path d="M15.5 8.5a5 5 0 0 1 0 7M18.5 5.5a9 9 0 0 1 0 13" /> : <path d="m16 9 5 5m0-5-5 5" />}
      </svg>
    );
  }
  return <svg aria-hidden="true" {...common}><path d="M21 11.5a8.4 8.4 0 0 1-9 8.5 9.7 9.7 0 0 1-4.1-.9L3 21l1.9-4.1A8.4 8.4 0 0 1 3 11.5a8.5 8.5 0 0 1 18 0Z" /></svg>;
}

export interface ChatWidgetProps {
  config: WidgetConfig;
  expiresAt: string;
  /** SR-7: optional tenant copy from the gateway admission response. */
  launcherLabel?: string;
  /** SR-3 decision 4: seeds the conversation thread from a resumed
   * sessionStorage record's `conversationId`. `null` on a fresh boot (no
   * resume in play) -- unchanged S14.2 behavior. */
  resumeConversationId?: string | null;
}

let messageIdCounter = 0;
function nextLocalId(): string {
  messageIdCounter += 1;
  return `local-${messageIdCounter}`;
}

/** Focusable-descendant query used by the hand-rolled focus trap (decision 1 —
 * no focus-trap library dependency, bundle-size rejection is locked). */
const FOCUSABLE_SELECTOR = [
  "a[href]",
  "button:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "[tabindex]:not([tabindex='-1'])",
].join(",");

function getFocusableElements(panel: HTMLElement): HTMLElement[] {
  return Array.from(panel.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)).filter(
    (el) => !el.hasAttribute("disabled"),
  );
}

const DEFAULT_LAUNCHER_LABEL = "Ask Rebecca";

interface VoiceRecognitionResultEvent {
  results: ArrayLike<{ 0: { transcript: string } }>;
}

interface VoiceRecognition {
  continuous: boolean;
  interimResults: boolean;
  lang: string;
  start: () => void;
  stop: () => void;
  abort: () => void;
  onstart: (() => void) | null;
  onend: (() => void) | null;
  onerror: (() => void) | null;
  onresult: ((event: VoiceRecognitionResultEvent) => void) | null;
}

type VoiceRecognitionConstructor = new () => VoiceRecognition;

function getVoiceRecognitionConstructor(): VoiceRecognitionConstructor | null {
  const browserWindow = window as typeof window & {
    SpeechRecognition?: VoiceRecognitionConstructor;
    webkitSpeechRecognition?: VoiceRecognitionConstructor;
  };
  return browserWindow.SpeechRecognition ?? browserWindow.webkitSpeechRecognition ?? null;
}

function connectionPresence(state: ConnectionState): string {
  if (state.kind === "online") return "Online";
  if (state.kind === "offline") return "Offline";
  if (state.kind === "session-expired") return "Session expired";
  if (state.kind === "rate-limited") return "Paused";
  return "Reconnecting";
}

export function ChatWidget({
  config,
  expiresAt,
  launcherLabel,
  resumeConversationId = null,
}: ChatWidgetProps) {
  const [open, setOpen] = useState(false);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [inputValue, setInputValue] = useState("");
  const [pending, setPending] = useState(false);
  const [schedulePending, setSchedulePending] = useState(false);
  const [scheduleError, setScheduleError] = useState(false);
  // In-memory only (S14.2 decision 4) — never persisted here (SR-3's
  // sessionStorage mirror, when opted in, lives in resume.ts, not this
  // ref). Seeded from resumeConversationId when a resume is in play (SR-3
  // decision 4), otherwise null exactly as before this sprint.
  const conversationIdRef = useRef<string | null>(resumeConversationId);
  // SR-3 decision 7: tracks whether the CURRENTLY in-flight/most-recent
  // conversation_id came from a resume, so a CONVERSATION_NOT_FOUND on that
  // specific turn can be distinguished from an ordinary mid-conversation
  // 404 (which keeps S14.2's plain error line, a regression guard).
  const resumedConversationInPlayRef = useRef<boolean>(resumeConversationId !== null);

  // S14.6: connection status + retry/reconnect state (decisions 1-7). All
  // in-memory (decision 7); nothing keyed by tenant_id.
  const [connectionState, setConnectionState] = useState<ConnectionState>({ kind: "online" });
  // Guards every retry/reconnect timer so no fetch fires once the component
  // is gone (decision 7 — the zombie-retry guard). Also gates against a
  // second concurrent send while a retry sequence is in flight.
  const unmountedRef = useRef(false);
  // Remembers the last failed send so the manual Retry control (shown once
  // auto-retry has stopped) can replay it without re-appending a duplicate
  // optimistic user bubble.
  const lastFailedSendRef = useRef<{ message: string; conversationId: string | null } | null>(null);
  // SR-14 D3: remembers the visitor's original question when the identity
  // gate fires (decision="identity_gate"), so a successful capture can
  // auto-re-send that EXACT message on the same conversation, with no
  // further visitor action. Cleared once consumed so a capture can never
  // re-send twice.
  const pendingIdentityQuestionRef = useRef<{ message: string; conversationId: string | null } | null>(null);
  const voiceRecognitionRef = useRef<VoiceRecognition | null>(null);
  const [listening, setListening] = useState(false);
  const voiceSupported = getVoiceRecognitionConstructor() !== null;

  useEffect(() => {
    return () => {
      unmountedRef.current = true;
      voiceRecognitionRef.current?.abort();
    };
  }, []);

  const panelRef = useRef<HTMLDivElement | null>(null);
  const launcherRef = useRef<HTMLButtonElement | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);
  // TTS: opt-in, in-memory-only mute preference (decision 5/6). Speaks only
  // once, on the first panel-open gesture in this page session.
  const [muted, setMuted] = useState(false);
  const hasGreetedRef = useRef(false);

  const toggleOpen = useCallback(() => {
    setOpen((prev) => {
      const next = !prev;
      if (next && !hasGreetedRef.current) {
        // First open in this page session is the user gesture that
        // satisfies the browser autoplay policy (load-bearing constraint
        // 2) — never speak before this.
        hasGreetedRef.current = true;
        if (!muted) {
          tts.speakGreeting();
        }
      }
      if (!next) {
        // Closing (whether via toggle or Escape) stops any in-flight
        // greeting speech so it doesn't keep talking into a closed panel.
        tts.cancel();
        voiceRecognitionRef.current?.stop();
        setListening(false);
      }
      return next;
    });
  }, [muted]);

  const toggleMuted = useCallback(() => {
    setMuted((prev) => {
      const next = !prev;
      if (next) {
        tts.cancel();
      }
      return next;
    });
  }, []);

  const toggleVoiceInput = useCallback(() => {
    if (listening) {
      voiceRecognitionRef.current?.stop();
      return;
    }

    const Recognition = getVoiceRecognitionConstructor();
    if (!Recognition) return;
    const recognition = new Recognition();
    recognition.continuous = false;
    recognition.interimResults = false;
    recognition.lang = document.documentElement.lang || "en-US";
    recognition.onstart = () => setListening(true);
    recognition.onend = () => setListening(false);
    recognition.onerror = () => setListening(false);
    recognition.onresult = (event) => {
      const transcript = event.results[0]?.[0]?.transcript?.trim();
      if (transcript) setInputValue(transcript);
    };
    voiceRecognitionRef.current = recognition;
    recognition.start();
  }, [listening]);

  // Focus-in on open (decision 1): move focus to the message input, the
  // first sensible target, once the panel mounts.
  useEffect(() => {
    if (open) {
      inputRef.current?.focus();
    }
  }, [open]);

  // Focus-restore on close (decision 1): return focus to the launcher.
  const wasOpenRef = useRef(false);
  useEffect(() => {
    if (!open && wasOpenRef.current) {
      launcherRef.current?.focus();
    }
    wasOpenRef.current = open;
  }, [open]);

  // Focus trap + Escape-to-close (decision 1): while open, Tab/Shift+Tab
  // cycle within the panel's focusable elements so keyboard focus can't
  // wander into the untrusted host page; Escape closes.
  const handlePanelKeyDown = useCallback(
    (event: React.KeyboardEvent<HTMLDivElement>) => {
      if (event.key === "Escape") {
        event.preventDefault();
        toggleOpen();
        return;
      }

      if (event.key !== "Tab") return;

      const panel = panelRef.current;
      if (!panel) return;
      const focusable = getFocusableElements(panel);
      if (focusable.length === 0) return;

      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const active = panel.ownerDocument.activeElement;

      if (event.shiftKey) {
        if (active === first || !panel.contains(active)) {
          event.preventDefault();
          last?.focus();
        }
      } else {
        if (active === last || !panel.contains(active)) {
          event.preventDefault();
          first?.focus();
        }
      }
    },
    [toggleOpen],
  );

  /**
   * Bounded expired-session reconnect (decision 5): a 401/403 mid-
   * conversation gets ONE bounded re-mint attempt sequence (small capped
   * count, no backoff loop beyond that cap) — never a silent unbounded
   * re-auth, since the re-mint itself hits the rate-limited
   * `/widget/session` endpoint. Returns true if a fresh session was
   * minted, false if the cap was hit (caller shows the honest
   * "please reload" state).
   */
  const attemptSessionReconnect = useCallback(async (): Promise<boolean> => {
    setConnectionState({ kind: "reconnecting-session" });
    for (let attempt = 1; attempt <= REMINT_MAX_ATTEMPTS; attempt += 1) {
      if (unmountedRef.current) return false;
      const admission = await mintVisitorSession(config);
      if (admission.ok) return true;
      console.error(
        `${LOG_PREFIX} session re-mint attempt ${attempt}/${REMINT_MAX_ATTEMPTS} failed: ${admission.error.errorCode}`,
      );
    }
    return false;
  }, [config]);

  // SR-3: a stable ref to the latest `runSend` closure, so the
  // RESUME_REJECTED recovery branch inside `runSend` itself can call it
  // again (a fresh conversation_id: null send) without a TDZ self-reference
  // on the `const runSend = useCallback(...)` binding.
  const runSendRef = useRef<(trimmed: string, conversationId: string | null) => Promise<void>>(
    async () => {},
  );

  const runSend = useCallback(
    async (trimmed: string, conversationId: string | null) => {
      setPending(true);
      lastFailedSendRef.current = null;

      let attemptCount = 0;
      const result = await withRetry<TurnResult>(
        () => {
          attemptCount += 1;
          return sendTurn(config, { message: trimmed, conversationId });
        },
        {
          maxAttempts: TURN_RETRY_MAX_ATTEMPTS,
          shouldAbort: () => unmountedRef.current,
          onRetry: ({ error }) => {
            if (unmountedRef.current) return;
            if (error.errorCode === "RATE_LIMITED") {
              setConnectionState({ kind: "rate-limited", retryAfterSeconds: error.retryAfterSeconds ?? null });
            } else {
              setConnectionState({ kind: "retrying" });
            }
          },
        },
      );

      if (unmountedRef.current) return;
      setPending(false);

      if (!result.ok) {
        const { errorCode, correlationId, status } = result.error;
        console.error(
          `${LOG_PREFIX} turn failed: ${errorCode} (status=${status ?? "n/a"}, correlation_id=${correlationId ?? "n/a"}, attempts=${attemptCount}): ${result.error.message}`,
        );

        // SR-3 decision 7 case (c): CONVERSATION_NOT_FOUND on a turn that
        // was carrying a RESUMED conversation_id (stale/foreign/rejected
        // handle) -- clear the bad resume record and silently continue as a
        // brand-new conversation from this same send (conversation_id:
        // null), never surfacing an error bubble or fabricating history.
        // An ordinary CONVERSATION_NOT_FOUND with no resume in play (should
        // not happen mid-conversation in practice, but is not this branch's
        // concern) falls through to the honest-failure path below unchanged
        // (S14.2 regression guard).
        if (errorCode === "CONVERSATION_NOT_FOUND" && resumedConversationInPlayRef.current) {
          console.error(`${LOG_PREFIX} resume rejected (RESUME_REJECTED): stale/foreign conversation_id, starting a new conversation.`);
          clearResumeRecord();
          resumedConversationInPlayRef.current = false;
          await runSendRef.current(trimmed, null);
          return;
        }

        // Bounded expired-session reconnect (decision 5) — only for an
        // actual auth failure, not every retryable transport error.
        if (status === 401 || status === 403) {
          const reconnected = await attemptSessionReconnect();
          if (unmountedRef.current) return;
          if (reconnected) {
            setConnectionState({ kind: "online" });
            lastFailedSendRef.current = { message: trimmed, conversationId };
            setMessages((prev) => [
              ...prev,
              {
                id: nextLocalId(),
                role: "system-error",
                text: "Your session was reconnected. Please send your message again.",
              },
            ]);
            return;
          }
          setConnectionState({ kind: "session-expired" });
          setMessages((prev) => [
            ...prev,
            {
              id: nextLocalId(),
              role: "system-error",
              text: "Your session expired. Please reload the page to continue.",
            },
          ]);
          return;
        }

        // Auto-retry exhausted (or a non-retryable business error) — honest
        // failure, never a fabricated reply (decision 1/3 constraint 3).
        setConnectionState({ kind: "offline" });
        lastFailedSendRef.current = { message: trimmed, conversationId };
        setMessages((prev) => [
          ...prev,
          {
            id: nextLocalId(),
            role: "system-error",
            text: "Sorry — something went wrong. Please try again.",
          },
        ]);
        return;
      }

      setConnectionState({ kind: "online" });
      conversationIdRef.current = result.turn.conversationId;
      // From here on, whatever conversation_id is in play was NOT
      // necessarily the originally-resumed one (it may be the fresh id from
      // a RESUME_REJECTED recovery above) -- either way it's now a normal,
      // server-confirmed thread; only a genuine future CONVERSATION_NOT_FOUND
      // on a still-resumed id should hit the special path again, so this
      // flag naturally reflects "was the id we just successfully used the
      // resumed one" via the caller's own tracking, not re-derived here.
      // SR-3 decision 8: only touch the persisted record when the tenant
      // opted in -- a no-op (and no sessionStorage write at all) otherwise.
      if (isResumeEnabled()) {
        touchResumeRecord(result.turn.conversationId, new Date());
      }
      // SR-14 D3: on an identity_gate reply, remember the visitor's ORIGINAL
      // message (the `trimmed` this send was for -- never the gate reply
      // itself) so a successful capture can auto-re-send it. Any other
      // decision clears a stale pending question, since the gate satisfying
      // itself means the NEXT unanswered message is what should be
      // remembered, not a leftover from an earlier gate turn.
      if (result.turn.decision === "identity_gate") {
        pendingIdentityQuestionRef.current = { message: trimmed, conversationId: result.turn.conversationId };
      } else {
        pendingIdentityQuestionRef.current = null;
      }
      const offersHumanHandoff = result.turn.decision === "escalate"
        && (result.turn.action === "schedule_cta" || result.turn.action === "lead_form");
      setMessages((prev) => [
        ...prev,
        {
          id: nextLocalId(),
          role: "bot",
          text: offersHumanHandoff ? SUPPORT_HANDOFF_REPLY : result.turn.reply,
          action: offersHumanHandoff ? "handoff_choice" : result.turn.action,
        },
      ]);
    },
    [config, attemptSessionReconnect],
  );

  useEffect(() => {
    runSendRef.current = runSend;
  }, [runSend]);

  const sendMessage = useCallback(async (message: string) => {
    const trimmed = message.trim();
    if (!trimmed || pending) return;

    const userMessage: ChatMessage = { id: nextLocalId(), role: "user", text: trimmed };
    setMessages((prev) => [...prev, userMessage]);
    await runSend(trimmed, conversationIdRef.current);
  }, [pending, runSend]);

  const handleSend = useCallback(async () => {
    const message = inputValue;
    voiceRecognitionRef.current?.stop();
    setListening(false);
    setInputValue("");
    await sendMessage(message);
  }, [inputValue, sendMessage]);

  const handleSuggestion = useCallback(async (message: string) => {
    await sendMessage(message);
  }, [sendMessage]);

  /**
   * The persistent "Connect with a sales rep" CTA (SR-5 decisions 4/5): a
   * pure client gesture, no `/public/chat/message` turn (no classify/
   * generate/cost). Appends a user bubble, then calls the new
   * server-authoritative availability-summary endpoint, then appends ONE
   * bot bubble carrying the fixed transition copy + the schedule action
   * (`schedule_cta` -> the staged in-thread picker via `<ScheduleCta>`;
   * `lead_form` -> the existing consent-gated lead form) -- both render
   * through `Bubble.tsx`'s existing action-dispatch, exactly like an
   * orchestrator-driven escalate, so the flow stays part of the scrollable
   * thread rather than a separate panel element (spec decision 5 / DoD).
   */
  const startScheduling = useCallback(async (userText = "Connect with a sales rep") => {
    if (pending || schedulePending) return;
    setScheduleError(false);
    setSchedulePending(true);
    setMessages((prev) => [...prev, { id: nextLocalId(), role: "user", text: userText }]);

    let result = await fetchAvailabilitySummary(config);
    if (unmountedRef.current) return;

    // Bounded expired-session reconnect (mirrors runSend's decision 5): a
    // 401/403 on the CTA click gets the SAME one-shot re-mint-and-retry as
    // an ordinary turn, instead of surfacing an honest failure for a merely
    // stale token. Never an unbounded loop -- attemptSessionReconnect itself
    // caps at REMINT_MAX_ATTEMPTS.
    if (!result.ok && (result.error.status === 401 || result.error.status === 403)) {
      const reconnected = await attemptSessionReconnect();
      if (unmountedRef.current) return;
      if (reconnected) {
        setConnectionState({ kind: "online" });
        result = await fetchAvailabilitySummary(config);
        if (unmountedRef.current) return;
      } else {
        setConnectionState({ kind: "session-expired" });
      }
    }

    setSchedulePending(false);
    if (!result.ok) {
      const { errorCode, correlationId, status } = result.error;
      console.error(
        `${LOG_PREFIX} fetchAvailabilitySummary failed: ${errorCode} (status=${status ?? "n/a"}, correlation_id=${correlationId ?? "n/a"})`,
      );
      setScheduleError(true);
      return;
    }
    setMessages((prev) => [
      ...prev,
      {
        id: nextLocalId(),
        role: "bot",
        text: result.summary.transitionMessage,
        action: result.summary.action,
        scheduleSummary: result.summary,
      },
    ]);
  }, [config, pending, schedulePending, attemptSessionReconnect]);

  const stayWithRebecca = useCallback(() => {
    if (pending || schedulePending) return;
    setMessages((prev) => [
      ...prev,
      { id: nextLocalId(), role: "user", text: "Stay with Rebecca" },
      { id: nextLocalId(), role: "bot", text: SUPPORT_STAY_REPLY },
    ]);
  }, [pending, schedulePending]);

  /** Manual Retry (decision 4/6): replay the last failed send without a new optimistic bubble. */
  const handleManualRetry = useCallback(async () => {
    const last = lastFailedSendRef.current;
    if (!last || pending) return;
    setConnectionState({ kind: "retrying" });
    await runSend(last.message, last.conversationId);
  }, [pending, runSend]);

  /**
   * SR-14 D3: on a successful identity capture, automatically re-send the
   * visitor's deferred original question on the SAME conversation, via the
   * existing `runSendRef` pattern (the same mechanism SR-3's
   * RESUME_REJECTED recovery already uses to programmatically re-invoke a
   * send from inside the widget) -- no new re-send mechanism is invented.
   * Consumes (clears) the pending question first so a duplicate
   * `onCaptured` call (there should never be one, but this is the
   * exactly-once guarantee `<IdentityForm>` promises) can never re-send
   * twice. A no-op if there is no pending question to answer (defensive;
   * should not happen since `<IdentityForm>` only renders on a gate turn).
   */
  const handleIdentityCaptured = useCallback(() => {
    const pending_ = pendingIdentityQuestionRef.current;
    if (!pending_) return;
    pendingIdentityQuestionRef.current = null;
    void runSendRef.current(pending_.message, pending_.conversationId);
  }, []);

  const handleKeyDown = useCallback(
    (event: React.KeyboardEvent<HTMLInputElement>) => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        void handleSend();
      }
    },
    [handleSend],
  );

  const resetChat = useCallback(() => {
    voiceRecognitionRef.current?.abort();
    setListening(false);
    setMessages([]);
    setInputValue("");
    setPending(false);
    setSchedulePending(false);
    setScheduleError(false);
    conversationIdRef.current = null;
    resumedConversationInPlayRef.current = false;
    lastFailedSendRef.current = null;
    pendingIdentityQuestionRef.current = null;
    if (isResumeEnabled()) clearResumeRecord();
    inputRef.current?.focus();
  }, []);

  const resolvedLauncherLabel = launcherLabel?.trim()
    ? launcherLabel
    : DEFAULT_LAUNCHER_LABEL;

  return (
    <>
      {open && (
        <div
          className="cw-panel"
          role="dialog"
          aria-modal="true"
          aria-labelledby={PANEL_HEADER_ID}
          ref={panelRef}
          onKeyDown={handlePanelKeyDown}
        >
          <div className="cw-panel-header">
            <span className="cw-assistant-mark" aria-hidden="true" />
            <span className="cw-panel-title">
              <span id={PANEL_HEADER_ID}>Rebecca <span className="cw-panel-role">&middot; AI assistant</span></span>
              <span className={`cw-panel-presence cw-panel-presence-${connectionState.kind}`}>
                <span className="cw-presence-dot" aria-hidden="true" />
                {connectionPresence(connectionState)}
              </span>
            </span>
            <span className="cw-header-actions">
              <button
                type="button"
                className="cw-header-button cw-reset-button"
                onClick={resetChat}
                disabled={pending || schedulePending}
                aria-label="Start a new chat"
                title="New chat"
              >
                <ChatGlyph name="reset" />
              </button>
              <button
                type="button"
                className="cw-header-button cw-mute-toggle"
                onClick={toggleMuted}
                aria-pressed={muted}
                aria-label={muted ? "Turn greeting sound on" : "Turn greeting sound off"}
              >
                <ChatGlyph name={muted ? "muted" : "sound"} />
              </button>
              <button type="button" className="cw-header-button cw-close-button" onClick={toggleOpen} aria-label="Close chat">
                <ChatGlyph name="close" />
              </button>
            </span>
          </div>
          <ConnectionStatus state={connectionState} onRetry={() => void handleManualRetry()} />
          <MessageList
            messages={messages}
            pending={pending}
            config={config}
            onSuggestion={(message) => void handleSuggestion(message)}
            onIdentityCaptured={handleIdentityCaptured}
            onHandoffTalk={() => void startScheduling("Talk to a rep")}
            onHandoffStay={stayWithRebecca}
          />
          {scheduleError && (
            <div className="cw-sched-error" role="alert">
              We couldn&rsquo;t check appointment availability. <button type="button" className="cw-sched-retry" onClick={() => void startScheduling()}>Retry</button>
            </div>
          )}
          <button
            type="button"
            className="cw-connect-sales-button"
            disabled={pending || schedulePending}
            onClick={() => void startScheduling()}
          >
            {schedulePending ? "Connecting…" : "Connect with a sales rep"}
          </button>
          <div className="cw-input-row">
            <div className="cw-composer">
              <input
              ref={inputRef}
              type="text"
              className="cw-input"
                placeholder={listening ? "Listening…" : "Message Rebecca…"}
              value={inputValue}
              disabled={pending}
              onChange={(e) => setInputValue(e.target.value)}
              onKeyDown={handleKeyDown}
                aria-label="Message"
              />
              {voiceSupported && (
                <button
                  type="button"
                  className={`cw-voice-button${listening ? " cw-voice-button-active" : ""}`}
                  onClick={toggleVoiceInput}
                  aria-pressed={listening}
                  aria-label={listening ? "Stop voice input" : "Start voice input"}
                  title="Voice input"
                >
                  <ChatGlyph name="mic" />
                </button>
              )}
            <button
              type="button"
              className="cw-send-button"
              disabled={pending || inputValue.trim().length === 0}
              onClick={() => void handleSend()}
              aria-label="Send message"
            >
              <ChatGlyph name="send" />
            </button>
            </div>
            <p className="cw-disclaimer">
              Rebecca is AI and can make mistakes <span aria-hidden="true">&middot;</span>{" "}
              <a className="cw-privacy-link" href="/privacy">Privacy policy</a>
            </p>
          </div>
        </div>
      )}
      <button
        type="button"
        className="cw-placeholder cw-launcher"
        ref={launcherRef}
        onClick={toggleOpen}
        aria-label={open ? "Close chat" : "Open chat"}
        aria-expanded={open}
        data-expires-at={expiresAt}
      >
        {open ? (
          <ChatGlyph name="close" />
        ) : (
          <span className="cw-launcher-orb" aria-hidden="true" />
        )}
        <span className="cw-launcher-label">{resolvedLauncherLabel}</span>
      </button>
    </>
  );
}

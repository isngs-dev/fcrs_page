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
 * Opens automatically on mount (user request, post-S14.6): the panel is
 * visible immediately, no launcher click required. The TTS greeting is
 * ALSO attempted on that same mount (user request, superseding the
 * original "gate behind a real click" stance -- see the S14.5 note below
 * and the mount effect above `handleLauncherClick`) so it is audible on page load in
 * browsers that allow it. Chrome does not: it requires prior user
 * activation on the frame before `speechSynthesis.speak()` will actually
 * produce audio, and mount time genuinely has none yet -- no application
 * code can force it to be heard at that point. `tts.speakGreeting()`
 * reports this back via its `onBlocked` callback (see tts.ts), which resets
 * `hasGreetedRef` so a second, document-level effect below can retry on the
 * visitor's first interaction ANYWHERE on the page (click/keydown/
 * touchstart, not necessarily on the widget itself) -- that interaction
 * grants the frame activation Chrome requires, making this the earliest
 * point real audio is achievable there. This is still best-effort, not a
 * guarantee: a visitor who never interacts with the page at all will never
 * hear it, by design of the browser policy, not a bug here.
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
 *
 * Exit-confirm adds: EVERY path that used to close the panel directly --
 * the header's close (X) button, the launcher acting as an X while open
 * (CSS-hidden while open, `.cw-placeholder[aria-expanded="true"]` in
 * widgetCss.ts, but still wired defensively), and Escape -- now routes
 * through `requestClose()`, which shows an in-panel exit-confirmation view
 * (`confirmingClose` state) instead of closing immediately. "Yes"
 * (`confirmCloseYes`) is the ONLY function that actually sets `open` to
 * false, and it also clears every piece of CLIENT-SIDE chat state (this
 * component's in-memory `messages`/refs + the SR-3 `resume.ts` sessionStorage
 * mirror) so the next reopen starts a genuinely fresh chat with a brand-new
 * `conversation_id` server-side. "No" (`confirmCloseNo`), or dismissing via
 * Escape while the confirmation itself is showing, changes nothing and
 * returns to the normal panel view. This NEVER touches server-side data:
 * nothing here writes to or deletes from the `conversation_store` the admin
 * dashboard reads from (see `conversation-store` skill) -- clearing is
 * 100% client-side (this component's state + one sessionStorage key), so
 * admin history for this same conversation remains fully intact. Because
 * `resume.ts`'s sessionStorage record is inherently tab-scoped (the Web
 * Storage API never shares `sessionStorage` across tabs/windows, even for
 * the same site), exiting in one tab can never clear or otherwise affect
 * chat state the visitor has open in a different tab -- each tab's widget
 * instance owns only its own in-memory state and its own sessionStorage.
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
import { MessageList, BOOK_CALL_SUGGESTION_MESSAGE } from "./MessageList";
import { ConnectionStatus, type ConnectionState } from "./ConnectionStatus";

const LOG_PREFIX = "[chatbot-widget]";
const PANEL_HEADER_ID = "cw-panel-header";
const CONFIRM_CLOSE_TITLE_ID = "cw-confirm-close-title";
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

interface VoiceRecognitionErrorEvent {
  error: string;
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
  onerror: ((event: VoiceRecognitionErrorEvent) => void) | null;
  onresult: ((event: VoiceRecognitionResultEvent) => void) | null;
}

/**
 * Maps the Web Speech API's `SpeechRecognitionErrorEvent.error` codes to an
 * honest, visitor-facing message -- or `null` for the two codes that are
 * NOT real failures ("no-speech": the listening window closed with nothing
 * said; "aborted": `stop()`/`abort()` was called, e.g. the visitor's own
 * click or the panel closing) and must stay silent, not alarm the visitor
 * over normal behavior.
 */
function voiceErrorMessage(code: string): string | null {
  switch (code) {
    case "no-speech":
    case "aborted":
      return null;
    case "not-allowed":
    case "service-not-allowed":
      return "Microphone access was denied. Please allow microphone access in your browser to use voice input.";
    case "audio-capture":
      return "No microphone was found. Please check your device and try again.";
    case "network":
      return "Voice input needs an internet connection. Please try again.";
    default:
      return "Voice input isn't available right now. Please type your message instead.";
  }
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
  // Opens automatically on mount (no launcher click required).
  const [open, setOpen] = useState(true);
  // Exit-confirm: true while the "Are you sure you want to exit?" view is
  // showing in place of the normal panel body (see requestClose/
  // confirmCloseYes/confirmCloseNo below). Separate from `open` -- the
  // panel stays open (mounted) throughout the confirmation; only a genuine
  // "Yes" flips `open` to false.
  const [confirmingClose, setConfirmingClose] = useState(false);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [inputValue, setInputValue] = useState("");
  const [pending, setPending] = useState(false);
  const [schedulePending, setSchedulePending] = useState(false);
  const [scheduleError, setScheduleError] = useState(false);
  // SR-27: hides the persistent "Connect with a sales rep" CTA once this
  // visitor already has a booking -- showing it afterward reads as "did my
  // booking not go through?" Two ways this becomes true, covering both
  // scheduling backends:
  //   1. handleBooked (below) -- ScheduleCta's own real, synchronous `201`
  //      confirmation. Fires immediately, same session.
  //   2. The one-time existingBooking re-check effect below -- the ONLY
  //      signal available for Calendly's hosted handoff (an external tab,
  //      no callback into this widget; the real booking is only knowable
  //      once Calendly's webhook has ingested it server-side) and also
  //      what correctly reflects a booking made in a PRIOR session on
  //      reopen/reload for either backend.
  const [hasBooking, setHasBooking] = useState(false);
  const handleBooked = useCallback(() => setHasBooking(true), []);
  const bookingCheckedRef = useRef(false);
  // Hides the SAME persistent "Connect with a sales rep" CTA while the
  // native in-thread <ScheduleCta> calendar/grid picker is already showing
  // as the most recent bot bubble -- a second, redundant "start scheduling"
  // button sitting directly beneath a calendar the visitor is already using
  // reads as confusing/duplicated, not helpful. Only the LAST message is
  // checked (not any earlier schedule_cta bubble from prior turns in the
  // same conversation) since that's the only one still visually active at
  // the bottom of the thread. Independent of hasBooking -- this covers the
  // in-progress, not-yet-booked window; hasBooking covers after.
  //
  // Deliberately does NOT include "calendly_handoff": that flow hands off
  // to an external tab with no reliable in-widget "booking confirmed"
  // signal (see hasBooking's own doc above), so the CTA staying visible
  // there is an existing, intentional, already-tested design decision --
  // not the same "avoid a redundant button under an active native picker"
  // problem this fixes.
  const lastMessage = messages[messages.length - 1];
  const schedulingUiActive = lastMessage?.role === "bot" && lastMessage.action === "schedule_cta";
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
  // Honest voice-input error (free, browser-native SpeechRecognition --
  // previously failed 100% silently: onerror just reset `listening` with
  // no feedback at all, so a visitor whose mic permission was denied (or
  // any other real failure) saw the button revert with zero explanation,
  // reading as "broken". "no-speech"/"aborted" are NOT real failures (no
  // speech detected in the window / the visitor's own stop) and never set
  // this -- only genuine problems (denied permission, no mic hardware, no
  // network) do.
  const [voiceError, setVoiceError] = useState<string | null>(null);
  const voiceSupported = getVoiceRecognitionConstructor() !== null;

  useEffect(() => {
    return () => {
      unmountedRef.current = true;
      voiceRecognitionRef.current?.abort();
    };
  }, []);

  // SR-27: one-time existingBooking check on mount (the panel opens
  // automatically, see `open`'s own comment above -- there is no separate
  // "first real open" moment to gate this on). Read-only, best-effort: a
  // failure here just leaves the CTA visible (the safe default) rather than
  // surfacing an error for a non-essential background check. Guarded by a
  // ref, not state, so it fires exactly once even if `config` identity
  // happens to change.
  useEffect(() => {
    if (bookingCheckedRef.current) return;
    bookingCheckedRef.current = true;
    void (async () => {
      const result = await fetchAvailabilitySummary(config);
      if (unmountedRef.current) return;
      if (result.ok && result.summary.existingBooking !== null) {
        setHasBooking(true);
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const panelRef = useRef<HTMLDivElement | null>(null);
  const launcherRef = useRef<HTMLButtonElement | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);
  // TTS: opt-in, in-memory-only mute preference (decision 5/6). Speaks only
  // once per page session, attempted from three possible triggers, in order
  // — mount (the panel auto-opens with no click required, see the effect
  // below), the visitor's first interaction ANYWHERE on the page (the
  // document-level effect further below — Chrome requires this before
  // `speechSynthesis.speak()` will actually produce audio, see tts.ts), or a
  // manual open, whichever happens first. `hasGreetedRef` gates all three so
  // only one genuinely fires; `tts.speakGreeting`'s `onBlocked` callback
  // resets it when an attempt was silently blocked, so a later trigger still
  // gets a real chance.
  const [muted, setMuted] = useState(false);
  const hasGreetedRef = useRef(false);

  const attemptGreeting = useCallback(() => {
    if (hasGreetedRef.current || muted) return;
    hasGreetedRef.current = true;
    tts.speakGreeting(() => {
      // Blocked (most commonly Chrome's "no speak() without prior user
      // activation on this frame" policy) — allow the next trigger (the
      // first-interaction listener below, or a manual open) to retry.
      hasGreetedRef.current = false;
    });
  }, [muted]);

  useEffect(() => {
    if (open) {
      attemptGreeting();
    }
    // Mount-only: a later `open`/`muted` change must never retroactively
    // speak or re-trigger this effect — attemptGreeting's own callers
    // (handleLauncherClick, the interaction listener below) own everything
    // after mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Fallback for browsers (Chrome) that block the mount-time attempt above
  // for lack of prior user activation: the visitor's first click, keypress,
  // or tap anywhere on the page — not necessarily on the widget — grants
  // that activation, so retry at that moment. `attemptGreeting` itself
  // no-ops if a prior attempt already genuinely spoke, so this is a no-op
  // in browsers where the mount-time attempt already worked. Capture phase
  // so this fires even if some other handler stops bubble-phase propagation.
  useEffect(() => {
    const events: Array<keyof DocumentEventMap> = ["click", "keydown", "touchstart"];
    const handleFirstInteraction = () => attemptGreeting();
    events.forEach((type) =>
      document.addEventListener(type, handleFirstInteraction, { once: true, capture: true }),
    );
    return () => {
      events.forEach((type) =>
        document.removeEventListener(type, handleFirstInteraction, { capture: true }),
      );
    };
  }, [attemptGreeting]);

  // "Hear it back": speak each new bot reply once, gated on !muted -- the
  // single choke point for every branch that appends a bot message (normal
  // reply, error fallback, scheduling handoff, support-stay, ...), rather
  // than a `tts.speak()` call duplicated at each of those call sites.
  // `lastSpokenIdRef` always advances to the latest message's id (even when
  // muted/non-bot) so a later unmute never retroactively speaks a reply
  // that already scrolled by.
  const lastSpokenIdRef = useRef<string | null>(null);
  useEffect(() => {
    const last = messages[messages.length - 1];
    if (!last || last.id === lastSpokenIdRef.current) return;
    lastSpokenIdRef.current = last.id;
    if (last.role === "bot" && last.text.trim() && !muted) {
      tts.speak(last.text);
    }
  }, [messages, muted]);

  /**
   * Show the exit confirmation instead of closing immediately. Every
   * close-intent trigger (header X, the launcher-as-X, Escape) calls this,
   * never `setOpen(false)` directly -- see confirmCloseYes, the only place
   * that actually closes. Stops voice input + any in-flight greeting speech
   * right away (not deferred to Yes/No): the confirmation view replaces the
   * whole panel body below, so the mic button and greeting audio are gone
   * from the screen the instant this fires regardless of what the visitor
   * decides next. Idempotent: calling this while already confirming is a
   * harmless no-op (React bails out of a `setState` to an unchanged value),
   * which is what makes repeated close-button clicks safe -- there is only
   * ever one confirmation showing, never a stack of them.
   */
  const requestClose = useCallback(() => {
    tts.cancel();
    voiceRecognitionRef.current?.stop();
    setListening(false);
    setConfirmingClose(true);
  }, []);

  /**
   * "Yes" on the exit confirmation -- the ONLY function that actually closes
   * the panel. Clears every piece of CLIENT-SIDE chat history/state a
   * visitor could otherwise see again on reopen:
   *  - this component's in-memory `messages` (the visible bubbles) and the
   *    turn-related refs/state (`conversationIdRef`, pending/schedule
   *    state, the last-failed-send + pending-identity-question refs) --
   *    all gone the instant this returns, since none of it is persisted
   *    beyond the component's own lifetime except...
   *  - ...the SR-3 `resume.ts` sessionStorage mirror (`RESUME_KEY`),
   *    cleared via `clearResumeRecord()` so a reload/reopen in THIS tab
   *    can't silently resume the old `conversation_id` either.
   * `conversationIdRef.current = null` is what actually guarantees "starts
   * a completely fresh chat" on reopen: the next message sent (if any)
   * asks the backend for a brand-new `conversation_id`, entirely decoupled
   * from the one just exited -- it does not continue, edit, or delete that
   * old thread.
   *
   * This NEVER touches server-side data: nothing in this file (or
   * resume.ts, which only ever stores `{token, expiresAt, conversationId,
   * lastActive}` -- never message content) calls any endpoint that writes
   * to or deletes from the backend `conversation_store`. That old
   * conversation's rows are untouched and remain fully visible to
   * CLIENT_ADMIN/CLIENT_AGENT in the admin dashboard -- this is a purely
   * client-side "forget my local view of this chat", the same guarantee
   * the existing "New chat" reset button already gives (this is that same
   * clearing, plus actually closing the panel).
   */
  const confirmCloseYes = useCallback(() => {
    setMessages([]);
    setInputValue("");
    setPending(false);
    setSchedulePending(false);
    setScheduleError(false);
    setVoiceError(null);
    conversationIdRef.current = null;
    resumedConversationInPlayRef.current = false;
    lastFailedSendRef.current = null;
    pendingIdentityQuestionRef.current = null;
    // Client-side (this tab's sessionStorage) only -- see the doc comment
    // above and resume.ts's own header for why this can never affect the
    // admin dashboard or any other browser tab.
    if (isResumeEnabled()) clearResumeRecord();
    setConfirmingClose(false);
    setOpen(false);
  }, []);

  /** "No", or dismissing the confirmation (Escape while it's showing) --
   * changes nothing about the chat; just hides the dialog and returns to
   * the normal panel view (the focus-in effect below re-focuses the input,
   * chat history stays exactly as it was). */
  const confirmCloseNo = useCallback(() => {
    setConfirmingClose(false);
  }, []);

  /** Launcher click: opens when closed (unaffected by exit-confirm), or
   * requests a close when open -- in practice unreachable via a real
   * pointer while open (the launcher is CSS-hidden + `pointer-events: none`
   * then, widgetCss.ts's `.cw-placeholder[aria-expanded="true"]`), but
   * wired correctly regardless (e.g. a synthetic `.click()`, as this
   * suite's test helpers use). */
  const handleLauncherClick = useCallback(() => {
    if (open) {
      requestClose();
      return;
    }
    setOpen(true);
    // No-ops if a prior trigger already genuinely spoke — kept as a
    // fallback so a manual open still greets when nothing else has yet.
    attemptGreeting();
  }, [open, requestClose, attemptGreeting]);

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
    tts.cancel(); // barge-in: starting to talk stops any reply mid-speech
    setVoiceError(null);
    const recognition = new Recognition();
    recognition.continuous = false;
    recognition.interimResults = false;
    recognition.lang = document.documentElement.lang || "en-US";
    recognition.onstart = () => setListening(true);
    recognition.onend = () => setListening(false);
    recognition.onerror = (event) => {
      setListening(false);
      const message = voiceErrorMessage(event.error);
      if (message) setVoiceError(message);
    };
    recognition.onresult = (event) => {
      const transcript = event.results[0]?.[0]?.transcript?.trim();
      if (transcript) {
        setInputValue(transcript);
      } else {
        setVoiceError("Didn't catch that — try again or type your question.");
      }
    };
    voiceRecognitionRef.current = recognition;
    recognition.start();
  }, [listening]);

  // Focus-in on open (decision 1): move focus to the message input, the
  // first sensible target, once the panel mounts -- also fires when
  // returning to the normal panel view after "No"/dismissing the exit
  // confirmation (confirmingClose: true -> false), since that view swaps
  // the input out of the DOM entirely while showing.
  useEffect(() => {
    if (open && !confirmingClose) {
      inputRef.current?.focus();
    }
  }, [open, confirmingClose]);

  // Focus-into the exit confirmation when it appears: the "No" (safe/
  // non-destructive) button, not "Yes" -- a defensive default so a stray
  // Enter keypress right after the dialog opens can't accidentally confirm
  // the exit.
  const confirmNoRef = useRef<HTMLButtonElement | null>(null);
  useEffect(() => {
    if (confirmingClose) {
      confirmNoRef.current?.focus();
    }
  }, [confirmingClose]);

  // Focus-restore on close (decision 1): return focus to the launcher.
  const wasOpenRef = useRef(false);
  useEffect(() => {
    if (!open && wasOpenRef.current) {
      launcherRef.current?.focus();
    }
    wasOpenRef.current = open;
  }, [open]);

  // Focus trap + Escape (decision 1): while open, Tab/Shift+Tab cycle
  // within the panel's CURRENTLY RENDERED focusable elements -- which is
  // just the two exit-confirm buttons while confirmingClose is true, since
  // that view replaces the rest of the panel body entirely (see the JSX
  // below), so the trap naturally confines itself with no extra logic
  // here. Escape's meaning depends on which view is showing: while
  // confirming, Escape is a "dismiss" gesture (same as "No" -- keep the
  // chat open, unchanged); otherwise it REQUESTS a close, same as the
  // header X (never closes directly -- see requestClose's doc comment).
  const handlePanelKeyDown = useCallback(
    (event: React.KeyboardEvent<HTMLDivElement>) => {
      if (event.key === "Escape") {
        event.preventDefault();
        if (confirmingClose) {
          confirmCloseNo();
        } else {
          requestClose();
        }
        return;
      }

      if (event.key !== "Tab") return;

      const panel = panelRef.current;
      if (!panel) return;
      const focusable = getFocusableElements(panel);
      if (focusable.length === 0) return;

      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      // The widget mounts inside a Shadow DOM in production, where
      // `document.activeElement` only ever resolves to the shadow HOST --
      // never the real focused descendant -- which made every Tab press
      // look like focus had left the panel and yank it back to `first`.
      // `getRootNode()` returns the ShadowRoot when actually shadow-mounted
      // (its own `.activeElement` reflects the true focus target) and falls
      // back to the plain document in tests, which render without a shadow
      // root.
      const root = panel.getRootNode();
      const active = root instanceof ShadowRoot ? root.activeElement : panel.ownerDocument.activeElement;

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
    [confirmingClose, confirmCloseNo, requestClose],
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
      // Explicit booking requests (the "Book a call with sales" suggestion
      // chip and the persistent "Connect with a sales rep" button) never
      // reach this branch at all -- both go through `startScheduling`
      // directly (bypassing `sendMessage`/the orchestrator turn entirely),
      // straight to the calendar/lead-form card. So every `decision ===
      // "escalate"` turn that DOES land here is bot-initiated (low
      // confidence, off-topic, the turn-count cap, or a free-typed
      // scheduling request the classifier caught) -- those still get the
      // one-step `<SupportHandoff>` confirm interstitial ("Talk to a rep" /
      // "Stay here") before the calendar/lead-form opens, but the bubble
      // above it now always shows the server's real `reply` text -- never a
      // hardcoded client-side apology string.
      const offersHumanHandoff = result.turn.decision === "escalate"
        && (result.turn.action === "schedule_cta" || result.turn.action === "lead_form");
      setMessages((prev) => [
        ...prev,
        {
          id: nextLocalId(),
          role: "bot",
          text: result.turn.reply,
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

  /**
   * SR-* fix: the "Book a call with sales" suggestion chip is an explicit
   * booking request exactly like the persistent CTA button -- it must go
   * straight to the calendar/lead-form card via `startScheduling` (no
   * `/public/chat/message` turn, no classify/generate/cost), never through
   * `sendMessage`/the orchestrator. Every OTHER suggestion chip is a real
   * question and keeps going through `sendMessage`/the LLM path unchanged.
   */
  const handleSuggestion = useCallback(async (message: string) => {
    if (message === BOOK_CALL_SUGGESTION_MESSAGE) {
      await startScheduling(message);
      return;
    }
    await sendMessage(message);
  }, [sendMessage, startScheduling]);

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
    setVoiceError(null);
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
          role={confirmingClose ? "alertdialog" : "dialog"}
          aria-modal="true"
          aria-labelledby={confirmingClose ? CONFIRM_CLOSE_TITLE_ID : PANEL_HEADER_ID}
          ref={panelRef}
          onKeyDown={handlePanelKeyDown}
        >
          {confirmingClose ? (
            <div className="cw-confirm-close">
              <p id={CONFIRM_CLOSE_TITLE_ID} className="cw-confirm-close-message">
                Are you sure you want to exit?
              </p>
              <div className="cw-confirm-close-actions">
                <button
                  type="button"
                  className="cw-confirm-close-no"
                  ref={confirmNoRef}
                  onClick={confirmCloseNo}
                >
                  No
                </button>
                <button type="button" className="cw-confirm-close-yes" onClick={confirmCloseYes}>
                  Yes
                </button>
              </div>
            </div>
          ) : (
            <>
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
                  <button type="button" className="cw-header-button cw-close-button" onClick={requestClose} aria-label="Close chat">
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
                onBooked={handleBooked}
              />
              {scheduleError && (
                <div className="cw-sched-error" role="alert">
                  We couldn&rsquo;t check appointment availability. <button type="button" className="cw-sched-retry" onClick={() => void startScheduling()}>Retry</button>
                </div>
              )}
              {!hasBooking && !schedulingUiActive && (
                <button
                  type="button"
                  className="cw-connect-sales-button"
                  disabled={pending || schedulePending}
                  onClick={() => void startScheduling()}
                >
                  {schedulePending ? "Connecting…" : "Connect with a sales rep"}
                </button>
              )}
              {voiceError && (
                <div className="cw-voice-error" role="alert">
                  {voiceError}
                </div>
              )}
              <div className="cw-input-row">
                <div className="cw-composer">
                  <input
                  ref={inputRef}
                  type="text"
                  className="cw-input"
                    placeholder={listening ? "Listening…" : "Message Rebecca…"}
                  value={inputValue}
                  disabled={pending}
                  onChange={(e) => {
                    tts.cancel(); // barge-in: typing stops any reply mid-speech
                    setInputValue(e.target.value);
                  }}
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
            </>
          )}
        </div>
      )}
      <button
        type="button"
        className="cw-placeholder cw-launcher"
        ref={launcherRef}
        onClick={handleLauncherClick}
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

/**
 * Opt-in browser TTS greeting (S14.5 decision 5, scope item 6).
 *
 * Thin wrapper over the native Web Speech API (`window.speechSynthesis` +
 * `SpeechSynthesisUtterance`) — zero-dependency, zero-backend, purely
 * client-side. No third-party TTS service, no audio assets, no autoplay.
 *
 * `ChatWidget` attempts `speakGreeting()` on mount (the panel opens
 * automatically, no click required — user request). Chrome (and
 * Chromium-based browsers) actively enforce "no `speechSynthesis.speak()`
 * without prior user activation on this frame" and silently produce no
 * audio at all when that hasn't happened yet — there is a real chance mount
 * time is too early. That block is invisible to the caller: `speak()` does
 * not throw or reject synchronously, it just never starts, so the only
 * signal is the utterance's own `error` event firing instead of `start`.
 * `speakGreeting` accepts an optional `onBlocked` callback specifically so
 * `ChatWidget` can notice this and retry once the visitor's first real
 * interaction with the page has granted activation — see the effect above
 * `toggleOpen` in ChatWidget.tsx.
 *
 * Capability check + try/catch is load-bearing regardless: `window.speechSynthesis`
 * may be absent (older/locked-down browsers), and `speak()`/`cancel()` can
 * throw synchronously too (permissions, etc.) — any failure here must
 * degrade to a harmless no-op and never throw into the host page or affect
 * chat.
 */

/** Baked-in greeting text (decision 5) — no server-driven/per-tenant config yet (flagged). */
export const TTS_GREETING_TEXT = "Hi, I'm your assistant. How can I help?";

function getSpeechSynthesis(): SpeechSynthesis | null {
  if (typeof window === "undefined") return null;
  const synth = window.speechSynthesis;
  if (!synth || typeof synth.speak !== "function") return null;
  return synth;
}

/**
 * Speak the baked-in greeting exactly once, if the Web Speech API is
 * available. Callers gate this on "first open in this page session" and
 * "not muted" — this function itself does not track either; it only
 * guarantees capability-checked, exception-safe speech.
 *
 * `onBlocked`, if given, fires when the capability check fails outright OR
 * when the utterance's `error` event fires with a genuine block reason (e.g.
 * Chrome's "not-allowed" — no prior user activation on this frame). It does
 * NOT fire for "canceled"/"interrupted" — those mean `cancel()` (below) or a
 * newer `speak()` call stopped an utterance that was already genuinely
 * playing/queued, which is expected on panel close and must not be treated
 * as a block worth retrying (that previously caused the greeting to replay
 * on every reopen: close cuts the greeting off mid-sentence -> "blocked" ->
 * the next open's retry speaks it again, repeatedly). Callers use a genuine
 * `onBlocked` to know a retry (from a later, real user gesture) is worth
 * attempting.
 */
export function speakGreeting(onBlocked?: () => void): void {
  const synth = getSpeechSynthesis();
  if (!synth) {
    onBlocked?.();
    return;
  }

  try {
    const Utterance = window.SpeechSynthesisUtterance;
    if (typeof Utterance !== "function") {
      onBlocked?.();
      return;
    }
    const utterance = new Utterance(TTS_GREETING_TEXT);
    if (onBlocked) {
      utterance.onerror = (event) => {
        if (event.error === "canceled" || event.error === "interrupted") return;
        onBlocked();
      };
    }
    synth.speak(utterance);
  } catch {
    // Silent degradation — speech failing must never break or throw into
    // the host page, and must never affect chat (decision 5 / load-bearing
    // constraint 2).
    onBlocked?.();
  }
}

/** Cancel any in-progress/queued speech (e.g. on mute toggle or panel close). */
export function cancel(): void {
  const synth = getSpeechSynthesis();
  if (!synth) return;

  try {
    synth.cancel();
  } catch {
    // Silent degradation, same rationale as speakGreeting.
  }
}

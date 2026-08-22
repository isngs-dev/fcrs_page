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
 *
 * Voice: `pickFemaleVoice` below best-effort-selects a female voice (the
 * Web Speech API has no gender field, only free-text `.name`, so this is a
 * name-hint heuristic, not a guarantee — some browsers/OSes may not ship
 * any voice matching a hint, in which case this silently falls back to
 * the browser/OS default voice, same as before this existed).
 */

/** Baked-in greeting text (decision 5) — no server-driven/per-tenant config yet (flagged). */
export const TTS_GREETING_TEXT = "Hi, I'm Rebecca, how can I help?";

function getSpeechSynthesis(): SpeechSynthesis | null {
  if (typeof window === "undefined") return null;
  const synth = window.speechSynthesis;
  if (!synth || typeof synth.speak !== "function") return null;
  return synth;
}

/**
 * Preference-ordered, case-insensitive substrings matched against
 * `SpeechSynthesisVoice.name` to find a female voice for the greeting.
 * The Web Speech API has no gender field on `SpeechSynthesisVoice`, so
 * this is a best-effort heuristic over well-known voice names shipped by
 * the major platforms/browsers (Edge's neural voices, Windows/legacy
 * Edge, macOS/iOS/Safari, Chrome/Google) — ordered so voices widely
 * regarded as clear and natural-sounding are tried first. The trailing
 * "female" entry is a catch-all for any voice whose name says so
 * explicitly (e.g. "Google UK English Female"), regardless of platform.
 */
const FEMALE_VOICE_NAME_HINTS = [
  "aria", // Microsoft Edge neural (US)
  "jenny", // Microsoft Edge neural (US)
  "samantha", // macOS/iOS default (US)
  "zira", // Windows/legacy Edge (US)
  "hazel", // Windows/legacy Edge (GB)
  "susan", // Windows legacy (US)
  "karen", // macOS (AU)
  "victoria", // macOS (US)
  "moira", // macOS (IE)
  "tessa", // macOS (ZA)
  "fiona", // macOS/legacy (Scottish)
  "female",
];

/**
 * Best-effort female-voice pick for a warm, clear, professional greeting.
 * Returns `null` (never throws) when `getVoices` is unavailable or no
 * hint matches — callers leave `utterance.voice` unset in that case,
 * which falls back to the browser/OS default voice exactly as before.
 */
function pickFemaleVoice(synth: SpeechSynthesis): SpeechSynthesisVoice | null {
  if (typeof synth.getVoices !== "function") return null;
  let voices: SpeechSynthesisVoice[];
  try {
    voices = synth.getVoices();
  } catch {
    return null;
  }
  for (const hint of FEMALE_VOICE_NAME_HINTS) {
    const match = voices.find((voice) => voice.name.toLowerCase().includes(hint));
    if (match) return match;
  }
  return null;
}

/**
 * Speak arbitrary text once, if the Web Speech API is available. Callers
 * gate this on "not muted" — this function itself does not track that; it
 * only guarantees capability-checked, exception-safe speech. Shared by the
 * greeting (`speakGreeting` below) and by spoken bot replies (ChatWidget's
 * "hear it back" effect).
 *
 * `onBlocked`, if given, fires when the capability check fails outright OR
 * when the utterance's `error` event fires with a genuine block reason (e.g.
 * Chrome's "not-allowed" — no prior user activation on this frame). It does
 * NOT fire for "canceled"/"interrupted" — those mean `cancel()` (below) or a
 * newer `speak()` call stopped an utterance that was already genuinely
 * playing/queued (panel close, barge-in), which must not be treated as a
 * block worth retrying.
 */
export function speak(text: string, onBlocked?: () => void): void {
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
    const utterance = new Utterance(text);
    const femaleVoice = pickFemaleVoice(synth);
    if (femaleVoice) {
      utterance.voice = femaleVoice;
    }
    // Warm/clear/professional delivery: a slightly unhurried pace reads as
    // warmer and is easier to follow than the default rate. Pitch is left
    // at the selected voice's own default — forcing an artificial pitch
    // shift on a synthetic voice tends to sound worse, not warmer.
    utterance.rate = 0.95;
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

/** Speak the baked-in greeting exactly once — see `speak` above for the
 * shared mechanics and `onBlocked` semantics. */
export function speakGreeting(onBlocked?: () => void): void {
  speak(TTS_GREETING_TEXT, onBlocked);
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

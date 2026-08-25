/**
 * TTS: cloud (OpenAI, via the backend) with a browser-native fallback
 * (S14.5 decision 5, scope item 6; extended for cloud voice).
 *
 * `speak`/`speakGreeting` try `synthesizeSpeech` (voice.ts, ->
 * `POST /public/chat/speak`) first, IFF `session.ts#isVoiceTtsEnabled()`
 * says the backend has an OpenAI key configured. On ANY failure of that
 * call (not configured, network error, upstream failure) they fall back to
 * the ORIGINAL browser-native mechanism below (`window.speechSynthesis` +
 * `SpeechSynthesisUtterance`) -- zero-dependency, zero-backend, purely
 * client-side. This means: today (no key set) this module's behavior is
 * byte-for-byte what it was before cloud voice existed; once a key is set,
 * it upgrades automatically with no further widget change or redeploy.
 *
 * `ChatWidget` attempts `speakGreeting()` on mount (the panel opens
 * automatically, no click required — user request). Chrome (and
 * Chromium-based browsers) actively enforce "no `speechSynthesis.speak()` /
 * `<audio>.play()` without prior user activation on this frame" and
 * silently produce no audio at all when that hasn't happened yet — there is
 * a real chance mount time is too early. That block is invisible to the
 * caller either way: neither mechanism throws or rejects synchronously on
 * this specific failure, so the only signal is an error/rejection event
 * firing instead of a genuine start. `speakGreeting` accepts an optional
 * `onBlocked` callback specifically so `ChatWidget` can notice this and
 * retry once the visitor's first real interaction with the page has
 * granted activation — see the effect above `toggleOpen` in ChatWidget.tsx.
 *
 * Capability check + try/catch is load-bearing regardless: `window.speechSynthesis`
 * may be absent (older/locked-down browsers), and `speak()`/`cancel()` can
 * throw synchronously too (permissions, etc.) — any failure here must
 * degrade to a harmless no-op and never throw into the host page or affect
 * chat.
 *
 * Voice: `pickFemaleVoice` below best-effort-selects a female voice for the
 * BROWSER-NATIVE path only (the Web Speech API has no gender field, only
 * free-text `.name`, so this is a name-hint heuristic, not a guarantee —
 * some browsers/OSes may not ship any voice matching a hint, in which case
 * this silently falls back to the browser/OS default voice). The CLOUD path
 * has no equivalent concept -- its voice is a fixed choice made server-side
 * (`ELEVENLABS_VOICE_ID`), not selected per-utterance here.
 *
 * `speakGreeting` is the one exception to all of the above: its text never
 * changes, so it does not call `speak()`/`synthesizeSpeech`/`speechSynthesis`
 * at all. It plays a pre-generated, bundled audio file (`greetingAudio.ts`)
 * directly -- no network round-trip, no live OpenAI call, no per-visit
 * delay. See that module's own comment for how to regenerate it if the
 * greeting's text, voice, or speed ever changes.
 */
import { GREETING_AUDIO_DATA_URI } from "./greetingAudio";
import { synthesizeSpeech } from "./voice";
import { isVoiceTtsEnabled } from "./session";
import type { WidgetConfig } from "./config";

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

/** Currently-playing cloud `<audio>` element + its object URL, if any (module-
 * scoped, mirrors `speechSynthesis`'s own single-utterance-at-a-time model).
 * Tracked so `cancel()` can stop cloud playback too, not just `speechSynthesis`. */
let currentAudio: HTMLAudioElement | null = null;
let currentObjectUrl: string | null = null;

function stopCloudAudio(): void {
  if (currentAudio) {
    try {
      currentAudio.pause();
    } catch {
      // Silent degradation, same rationale as the rest of this module.
    }
    currentAudio.onended = null;
    currentAudio.src = "";
  }
  currentAudio = null;
  if (currentObjectUrl) {
    URL.revokeObjectURL(currentObjectUrl);
    currentObjectUrl = null;
  }
}

/** The original, fully browser-native speak path — unchanged from before
 * cloud voice existed, aside from `speed` (see `speak` below). See the
 * module doc comment for the fallback contract. */
function speakBrowserNative(text: string, onBlocked?: () => void, speed?: number): void {
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
    // shift on a synthetic voice tends to sound worse, not warmer. `speed`
    // (only ever passed by the greeting) overrides this baseline rate.
    utterance.rate = speed ?? 0.95;
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

/**
 * Speak arbitrary text once. Tries cloud TTS first when
 * `isVoiceTtsEnabled()`, falls back to the browser-native mechanism on any
 * failure (not configured, network error, upstream failure, or the
 * cloud audio being blocked by the same autoplay policy `speechSynthesis`
 * is subject to). Callers gate this on "not muted" — this function itself
 * does not track that; it only guarantees capability-checked,
 * exception-safe speech. Shared by the greeting (`speakGreeting` below) and
 * by spoken bot replies (ChatWidget's "hear it back" effect).
 *
 * `onBlocked`, if given, fires when EITHER mechanism's capability check
 * fails outright, or a genuine block reason occurs (Chrome's "not-allowed"
 * for `speechSynthesis`, or a rejected `<audio>.play()` for cloud audio —
 * both mean "no prior user activation on this frame yet"). It does NOT fire
 * for "canceled"/"interrupted" (`speechSynthesis`) — those mean `cancel()`
 * (below) or a newer `speak()` call stopped an utterance that was already
 * genuinely playing/queued (panel close, barge-in), which must not be
 * treated as a block worth retrying.
 *
 * `speed` (1.0 = normal) is an optional override applied to BOTH mechanisms
 * identically -- omitted by the "hear it back" reply-speaking call site
 * (each mechanism keeps its own existing default), passed explicitly only
 * by `speakGreeting` below for a slightly slower, warmer welcome.
 */
export async function speak(
  config: WidgetConfig,
  text: string,
  onBlocked?: () => void,
  speed?: number,
): Promise<void> {
  if (isVoiceTtsEnabled()) {
    const result = await synthesizeSpeech(config, text, speed);
    if (result.ok) {
      stopCloudAudio();
      const url = URL.createObjectURL(result.audio);
      const audio = new Audio(url);
      currentAudio = audio;
      currentObjectUrl = url;
      audio.onended = () => {
        if (currentAudio === audio) stopCloudAudio();
      };
      try {
        await audio.play();
      } catch {
        // Autoplay-policy block (or any other play() failure) -- same
        // "no activation yet" contract as speechSynthesis's onerror, NOT a
        // reason to also try the browser-native path (it would almost
        // certainly be blocked for the identical reason).
        stopCloudAudio();
        onBlocked?.();
      }
      return;
    }
    // Cloud call itself failed (not configured / network / upstream) --
    // fall through to the browser-native mechanism below.
  }
  speakBrowserNative(text, onBlocked, speed);
}

/**
 * Speak the baked-in greeting exactly once. Unlike `speak` above, this
 * NEVER calls `synthesizeSpeech` or `speechSynthesis` — `TTS_GREETING_TEXT`
 * is fixed, so its audio is pre-generated once (`greetingAudio.ts`, 0.85x
 * speed, the same voice/model the live TTS endpoint uses) and played
 * directly from the bundle. No network round-trip, no live OpenAI call, so
 * no per-visit synthesis delay before the visitor hears anything.
 *
 * `_config` is unused now (kept only so every existing call site —
 * `ChatWidget.tsx`, tests — doesn't need updating for an API shape that no
 * longer needs it).
 *
 * Still subject to the same browser autoplay-activation policy as any other
 * audio playback (a local `data:` URI is not exempt), so `onBlocked`'s
 * "retry on the visitor's next real interaction" contract is unchanged —
 * see `speak` above for the full semantics.
 */
export async function speakGreeting(_config: WidgetConfig, onBlocked?: () => void): Promise<void> {
  try {
    stopCloudAudio();
    const audio = new Audio(GREETING_AUDIO_DATA_URI);
    currentAudio = audio;
    audio.onended = () => {
      if (currentAudio === audio) stopCloudAudio();
    };
    await audio.play();
  } catch {
    // Blocked by autoplay policy, or any other playback failure — same
    // silent-degradation contract as the rest of this module.
    stopCloudAudio();
    onBlocked?.();
  }
}

/** Cancel any in-progress/queued speech, cloud or browser-native (e.g. on
 * mute toggle, panel close, or barge-in) — always both, unconditionally
 * safe/idempotent when the other mechanism was never in use. */
export function cancel(): void {
  stopCloudAudio();

  const synth = getSpeechSynthesis();
  if (!synth) return;
  try {
    synth.cancel();
  } catch {
    // Silent degradation, same rationale as speakBrowserNative.
  }
}

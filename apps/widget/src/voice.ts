/**
 * Cloud voice (ASR/TTS) calls to `POST /public/chat/transcribe` and
 * `POST /public/chat/speak` -- mirrors `turn.ts`'s pattern exactly: never
 * throws, every failure path returns a typed error, the React layer never
 * touches `fetch` directly. Both are optional upgrades over the widget's
 * own browser-native mechanisms (`SpeechRecognition`/`speechSynthesis`) --
 * callers (`ChatWidget.tsx`, `tts.ts`) fall back to those on any
 * `VoiceCallResult` failure, never surface a hard error to the visitor for
 * "cloud voice isn't configured/reachable".
 */
import { authHeader } from "./session";
import type { WidgetConfig } from "./config";

export interface VoiceCallError {
  readonly type: "VOICE_CALL_ERROR";
  readonly errorCode: string;
  readonly message: string;
}

export type TranscribeResult =
  | { ok: true; text: string }
  | { ok: false; error: VoiceCallError };

export type SpeakResult =
  | { ok: true; audio: Blob }
  | { ok: false; error: VoiceCallError };

interface BackendErrorEnvelope {
  error_code?: unknown;
  message?: unknown;
}

function parseErrorEnvelope(body: unknown): { errorCode: string; message: string } {
  if (body && typeof body === "object") {
    const envelope = body as BackendErrorEnvelope;
    return {
      errorCode: typeof envelope.error_code === "string" ? envelope.error_code : "UNKNOWN_ERROR",
      message: typeof envelope.message === "string" ? envelope.message : "Voice request failed.",
    };
  }
  return { errorCode: "UNKNOWN_ERROR", message: "Voice request failed." };
}

/**
 * Send one recorded utterance for transcription. `audio` is posted as the
 * raw request body (no multipart wrapper) with its own `type` as the
 * Content-Type header -- matching `api.voice.routes.post_transcribe`
 * exactly.
 */
export async function transcribeAudio(config: WidgetConfig, audio: Blob): Promise<TranscribeResult> {
  const auth = authHeader();
  if (!auth) {
    return {
      ok: false,
      error: { type: "VOICE_CALL_ERROR", errorCode: "NO_SESSION", message: "No visitor session is held." },
    };
  }

  let response: Response;
  try {
    response = await fetch(`${config.apiBase}/public/chat/transcribe`, {
      method: "POST",
      headers: { "Content-Type": audio.type || "audio/webm", ...auth },
      credentials: "omit",
      body: audio,
    });
  } catch (err) {
    return {
      ok: false,
      error: {
        type: "VOICE_CALL_ERROR",
        errorCode: "NETWORK_ERROR",
        message: err instanceof Error ? err.message : "Network request failed.",
      },
    };
  }

  if (!response.ok) {
    let body: unknown = null;
    try {
      body = await response.json();
    } catch {
      body = null;
    }
    const { errorCode, message } = parseErrorEnvelope(body);
    return { ok: false, error: { type: "VOICE_CALL_ERROR", errorCode, message } };
  }

  let body: unknown;
  try {
    body = await response.json();
  } catch {
    return {
      ok: false,
      error: { type: "VOICE_CALL_ERROR", errorCode: "INVALID_RESPONSE_SHAPE", message: "Transcribe response failed validation." },
    };
  }
  const text = body && typeof body === "object" ? (body as { text?: unknown }).text : undefined;
  if (typeof text !== "string") {
    return {
      ok: false,
      error: { type: "VOICE_CALL_ERROR", errorCode: "INVALID_RESPONSE_SHAPE", message: "Transcribe response failed validation." },
    };
  }
  return { ok: true, text };
}

/** Synthesize speech for `text`. Returns the raw audio as a `Blob`
 * (`audio/mpeg`) for the caller to play via an `<audio>` element. */
export async function synthesizeSpeech(config: WidgetConfig, text: string): Promise<SpeakResult> {
  const auth = authHeader();
  if (!auth) {
    return {
      ok: false,
      error: { type: "VOICE_CALL_ERROR", errorCode: "NO_SESSION", message: "No visitor session is held." },
    };
  }

  let response: Response;
  try {
    response = await fetch(`${config.apiBase}/public/chat/speak`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...auth },
      credentials: "omit",
      body: JSON.stringify({ text }),
    });
  } catch (err) {
    return {
      ok: false,
      error: {
        type: "VOICE_CALL_ERROR",
        errorCode: "NETWORK_ERROR",
        message: err instanceof Error ? err.message : "Network request failed.",
      },
    };
  }

  if (!response.ok) {
    let body: unknown = null;
    try {
      body = await response.json();
    } catch {
      body = null;
    }
    const { errorCode, message } = parseErrorEnvelope(body);
    return { ok: false, error: { type: "VOICE_CALL_ERROR", errorCode, message } };
  }

  const audio = await response.blob();
  return { ok: true, audio };
}

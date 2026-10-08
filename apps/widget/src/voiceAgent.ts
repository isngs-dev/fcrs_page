/**
 * "Call Us" -- a live browser call with the AI voice agent (server:
 * api.voice_agent). The API mints a short-lived Plivo JWT + a signed call
 * ticket; the Plivo Browser SDK (loaded only on click) registers with the JWT
 * and places the call with the ticket as an X-PH header, and Plivo turns the
 * caller's speech into text for the agent and speaks its answers. The
 * transcript is saved server-side on this chat's conversation.
 */
import { z } from "zod";

import type { WidgetConfig } from "./config";
import { authHeader } from "./session";

const StartCallSchema = z.object({
  call_id: z.string(),
  token: z.string(),
  ticket: z.string(),
  conversation_id: z.string(),
});

const CallStatusSchema = z.object({ status: z.string(), offer_booking: z.boolean() });

export interface VoiceCallHandle {
  callId: string;
  conversationId: string;
  hangUp(): void;
  setMuted(muted: boolean): void;
}

export interface VoiceCallEvents {
  onConnected(): void;
  /** Fired once, however the call ended (hang-up, transfer finished, error). */
  onEnded(): void;
}

export type StartVoiceCallResult = { ok: true; call: VoiceCallHandle } | { ok: false; message: string };

const UNAVAILABLE = "Sorry, we couldn't start the call. Please try again or use Schedule a Call.";
const MIC_BLOCKED = "Please allow microphone access to call us, or use Schedule a Call.";
const LOGIN_TIMEOUT_MS = 15_000;
// Any destination works: the Plivo Application's answer URL routes the call.
const DESTINATION = "voiceagent";

export async function startVoiceAgentCall(
  config: WidgetConfig,
  conversationId: string | null,
  events: VoiceCallEvents,
): Promise<StartVoiceCallResult> {
  const auth = authHeader();
  if (!auth) return { ok: false, message: UNAVAILABLE };

  let started: z.infer<typeof StartCallSchema>;
  try {
    const response = await fetch(`${config.apiBase}/public/voice-agent/calls`, {
      method: "POST",
      headers: { ...auth, "Content-Type": "application/json" },
      credentials: "omit",
      body: JSON.stringify({ conversation_id: conversationId }),
    });
    const parsed = StartCallSchema.safeParse(await response.json());
    if (!response.ok || !parsed.success) return { ok: false, message: UNAVAILABLE };
    started = parsed.data;
  } catch {
    return { ok: false, message: UNAVAILABLE };
  }

  try {
    // Ask for the mic up front so a refusal gets its own message.
    const mic = await navigator.mediaDevices.getUserMedia({ audio: true });
    mic.getTracks().forEach((track) => track.stop());

    const { default: Plivo } = await import("plivo-browser-sdk");
    const { client } = new Plivo({ debug: "ERROR", permOnClick: true, enableTracking: false });
    await new Promise<void>((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("Plivo login timed out")), LOGIN_TIMEOUT_MS);
      client.on("onLogin", () => {
        clearTimeout(timer);
        resolve();
      });
      client.on("onLoginFailed", (cause: unknown) => {
        clearTimeout(timer);
        reject(new Error(`Plivo login failed: ${String(cause)}`));
      });
      client.loginWithAccessToken(started.token);
    });

    let ended = false;
    const finish = () => {
      if (ended) return;
      ended = true;
      client.logout();
      events.onEnded();
    };
    client.on("onCallAnswered", () => events.onConnected());
    client.on("onCallTerminated", finish);
    client.on("onCallFailed", finish);
    if (!client.call(DESTINATION, { "X-PH-Ticket": started.ticket })) {
      client.logout();
      return { ok: false, message: UNAVAILABLE };
    }
    return {
      ok: true,
      call: {
        callId: started.call_id,
        conversationId: started.conversation_id,
        hangUp: () => client.hangup(),
        setMuted: (muted) => (muted ? client.mute() : client.unmute()),
      },
    };
  } catch (err) {
    const denied = err instanceof Error && /permission|notallowed|denied/i.test(`${err.name} ${err.message}`);
    return { ok: false, message: denied ? MIC_BLOCKED : UNAVAILABLE };
  }
}

/** After a call: did the visitor fail to reach the team (after hours / no
 * answer)? Then the widget opens the booking card. `false` on any error. */
export async function shouldOfferBookingAfterCall(config: WidgetConfig, callId: string): Promise<boolean> {
  const auth = authHeader();
  if (!auth) return false;
  try {
    const response = await fetch(`${config.apiBase}/public/voice-agent/calls/${encodeURIComponent(callId)}`, {
      headers: { ...auth },
      credentials: "omit",
    });
    const parsed = CallStatusSchema.safeParse(await response.json());
    return response.ok && parsed.success && parsed.data.offer_booking;
  } catch {
    return false;
  }
}

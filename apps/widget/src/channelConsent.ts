/**
 * Per-channel follow-up consent (Sprint A4 H1/H2).
 *
 * The wording here MUST match the server's registry for the same version
 * (`services/api/src/api/agents/consent_wording.py`): the server stores ITS
 * text for the version we send, so the version is what ties "what the
 * visitor saw" to "what is on record". Never edit a published version's
 * text -- add a new version on both sides.
 */
import { getOutreachConsent } from "./session";

export const CHANNEL_CONSENT_VERSION = "2026-10-a";
export const EMAIL_CONSENT_TEXT =
  "Email me about my enquiry, including follow-ups. I can unsubscribe at any time.";
export const CALL_CONSENT_TEXT =
  "You may call me at the number I provided about my enquiry, including with automated " +
  "technology and an AI assistant. Consent is not a condition of purchase; I can say stop at any time.";

export interface ChannelConsentOffer {
  email: boolean;
  call: boolean;
}

export interface ChannelConsentsPayload {
  email: boolean;
  call: boolean;
  version: string;
}

/** Which boxes to show: only what the tenant can use, and only when the
 * server's wording version is the one this bundle displays. */
export function channelConsentOffer(): ChannelConsentOffer | null {
  const offer = getOutreachConsent();
  if (!offer || offer.version !== CHANNEL_CONSENT_VERSION) return null;
  if (!offer.email && !offer.call) return null;
  return { email: offer.email, call: offer.call };
}

/** The visitor's IANA timezone (contact-local calling windows), if the browser knows it. */
export function browserTimeZone(): string | undefined {
  try {
    const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    return zone || undefined;
  } catch {
    return undefined;
  }
}

/** `{ timezone }` when the browser knows it, else `{}` (spread into a request body). */
export function timeZoneField(): { timezone?: string } {
  const zone = browserTimeZone();
  return zone ? { timezone: zone } : {};
}

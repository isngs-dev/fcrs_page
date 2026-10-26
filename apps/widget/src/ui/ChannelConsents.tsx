/**
 * Optional follow-up consent checkboxes (Sprint A4 H1). Unticked by default,
 * each with its own label (screen readers announce the full wording). The
 * call box is disabled until a phone number is entered -- the server rejects
 * call consent without one.
 */
import {
  CALL_CONSENT_TEXT,
  EMAIL_CONSENT_TEXT,
  type ChannelConsentOffer,
} from "../channelConsent";

export interface ChannelConsentValue {
  email: boolean;
  call: boolean;
}

export interface ChannelConsentsProps {
  idPrefix: string;
  offer: ChannelConsentOffer;
  value: ChannelConsentValue;
  onChange: (next: ChannelConsentValue) => void;
  disabled: boolean;
  hasPhone: boolean;
}

export function ChannelConsents({ idPrefix, offer, value, onChange, disabled, hasPhone }: ChannelConsentsProps) {
  return (
    <fieldset className="cw-channel-consents">
      <legend className="cw-channel-consents-legend">Follow-up (optional)</legend>
      {offer.email && (
        <div className="cw-lead-consent-row">
          <input
            id={`${idPrefix}-consent-email`}
            className="cw-lead-checkbox"
            type="checkbox"
            checked={value.email}
            disabled={disabled}
            onChange={(e) => onChange({ ...value, email: e.target.checked })}
          />
          <label className="cw-lead-consent-label" htmlFor={`${idPrefix}-consent-email`}>
            {EMAIL_CONSENT_TEXT}
          </label>
        </div>
      )}
      {offer.call && (
        <div className="cw-lead-consent-row">
          <input
            id={`${idPrefix}-consent-call`}
            className="cw-lead-checkbox"
            type="checkbox"
            checked={value.call && hasPhone}
            disabled={disabled || !hasPhone}
            aria-describedby={hasPhone ? undefined : `${idPrefix}-consent-call-hint`}
            onChange={(e) => onChange({ ...value, call: e.target.checked })}
          />
          <label className="cw-lead-consent-label" htmlFor={`${idPrefix}-consent-call`}>
            {CALL_CONSENT_TEXT}
          </label>
          {!hasPhone && (
            <span id={`${idPrefix}-consent-call-hint`} className="cw-channel-consents-hint">
              Add a phone number to allow calls.
            </span>
          )}
        </div>
      )}
    </fieldset>
  );
}

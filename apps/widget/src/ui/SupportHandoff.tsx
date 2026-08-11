import { useState } from "react";

export interface SupportHandoffProps {
  disabled?: boolean;
  onTalk: () => void;
  onStay: () => void;
}

function RepresentativeGlyph() {
  return (
    <svg aria-hidden="true" width="25" height="25" viewBox="0 0 24 24" fill="none">
      <circle cx="12" cy="7.5" r="3.5" fill="currentColor" opacity=".88" />
      <path d="M5.5 20v-2.3A6.5 6.5 0 0 1 12 11.2a6.5 6.5 0 0 1 6.5 6.5V20" fill="currentColor" opacity=".72" />
      <path d="M9 13.2 12 17l3-3.8M12 17v3" stroke="#fff" strokeWidth="1.35" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function SupportHandoff({ disabled = false, onTalk, onStay }: SupportHandoffProps) {
  const [choice, setChoice] = useState<"talk" | "stay" | null>(null);
  const unavailable = disabled || choice !== null;

  function chooseTalk() {
    if (unavailable) return;
    setChoice("talk");
    onTalk();
  }

  function chooseStay() {
    if (unavailable) return;
    setChoice("stay");
    onStay();
  }

  return (
    <section className="cw-handoff-card" aria-label="Connect with a representative">
      <div className="cw-handoff-heading">
        <span className="cw-handoff-avatar" aria-hidden="true"><RepresentativeGlyph /></span>
        <span className="cw-handoff-copy">
          <strong>Connect with a rep</strong>
          <span>Book a time that suits you</span>
        </span>
      </div>
      <div className="cw-handoff-actions">
        <button
          type="button"
          className={`cw-handoff-talk${choice === "talk" ? " cw-handoff-talk-selected" : ""}`}
          disabled={unavailable}
          aria-pressed={choice === "talk"}
          onClick={chooseTalk}
        >
          Talk to a rep
        </button>
        <button
          type="button"
          className={`cw-handoff-stay${choice === "stay" ? " cw-handoff-stay-selected" : ""}`}
          disabled={unavailable}
          aria-pressed={choice === "stay"}
          onClick={chooseStay}
        >
          Stay here
        </button>
      </div>
    </section>
  );
}

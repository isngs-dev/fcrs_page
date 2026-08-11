/**
 * Ink & Citron visual system for the Shadow DOM widget. It intentionally
 * lives in a TS string so the production embed remains one self-contained
 * file and none of these rules can leak into a client's host page.
 *
 * Restyled to HANDOFF-SPEC.md §1/§2 ("Widget (350×520 panel)") and the "3b"
 * canonical states in `Chatbot System Designs.dc.html` — visual tokens only;
 * no class names were renamed and no DOM/behavior changed (see ChatWidget.tsx,
 * MessageList.tsx, Bubble.tsx, LeadForm.tsx, ScheduleCta.tsx, ConnectionStatus.tsx
 * for the untouched structure/logic these rules target).
 */
import geistLatinDataUri from "../assets/fonts/geist-latin.woff2?inline";

/** The real SIL OFL-licensed Geist Latin variable font is inlined by Vite at
 * build time, so the visitor's host page never makes a font request. */
export const widgetCss = `
@font-face {
  font-family: "Geist";
  font-style: normal;
  font-weight: 100 900;
  font-display: swap;
  src: url("${geistLatinDataUri}") format("woff2");
}

:host {
  all: initial;
  --cw-ink: #0f172a;
  --cw-citron: #2563eb;
  --cw-citron-soft: #dbeafe;
  --cw-paper: #ffffff;
  --cw-cool-paper: #f8fafc;
  --cw-line: #e5e7eb;
  --cw-line-dashed: #cbd5e1;
  --cw-text-secondary: #1e293b;
  --cw-muted: #64748b;
  --cw-dim: #94a3b8;
  --cw-faint: #64748b;
  --cw-success: #15803d;
  --cw-success-bg: #ecfdf3;
  --cw-warning-bg: #fff9ec;
  --cw-warning-line: #f0e2bd;
  --cw-warning-ink: #6a4e00;
  --cw-danger-ink: #79221d;
  --cw-danger-bg: #fff1ef;
  --cw-danger-line: #d99b95;
  --cw-online: #22c55e;
  font-family: "Geist", ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  color: var(--cw-ink);
  line-height: 1.45;
}

*, *::before, *::after { box-sizing: border-box; }
button, input { font: inherit; }
button { -webkit-tap-highlight-color: transparent; touch-action: manipulation; }

/* Launcher — 56px ink circle, citron icon (HANDOFF-SPEC §2 Widget: Launcher) */
.cw-placeholder {
  position: fixed;
  right: 28px;
  bottom: 28px;
  z-index: 2147483000;
  width: auto;
  min-height: 54px;
  display: inline-flex;
  align-items: center;
  justify-content: flex-start;
  gap: 11px;
  max-width: calc(100vw - 40px);
  padding: 14px 22px 14px 18px;
  border: none;
  border-radius: 999px;
  background: var(--cw-citron);
  color: var(--cw-paper);
  box-shadow: 0 14px 30px -8px rgba(37, 99, 235, .6);
  font-size: 14px;
  font-weight: 700;
  letter-spacing: -0.01em;
  cursor: pointer;
  animation: cw-launcher-pop 300ms ease-out;
  transition: transform 180ms ease, box-shadow 180ms ease, background 180ms ease;
}
.cw-placeholder:hover { background: #1d4ed8; box-shadow: 0 16px 34px -8px rgba(37, 99, 235, .7); }
.cw-placeholder:active { transform: scale(0.97); }
.cw-placeholder[aria-expanded="true"] { visibility: hidden; opacity: 0; pointer-events: none; }
.cw-launcher-orb {
  position: relative;
  width: 26px;
  height: 26px;
  flex: 0 0 auto;
  border-radius: 8px;
  background: conic-gradient(from 45deg, #38bdf8, #818cf8, #c084fc, #38bdf8);
  animation: cw-orb-spin 6s linear infinite;
}
.cw-launcher-orb::after {
  content: "";
  position: absolute;
  inset: 6px;
  border-radius: 50%;
  background: rgba(255,255,255,.9);
}
/* Desktop uses an icon-and-text pill. The label collapses visually only on
   narrow screens, while the button's accessible name remains unchanged. */
.cw-launcher-label {
  min-width: 0;
  overflow: hidden;
  color: var(--cw-paper);
  font-size: 15.5px;
  font-weight: 600;
  line-height: 1;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.cw-placeholder:focus-visible, .cw-input:focus-visible, .cw-suggestion:focus-visible,
.cw-header-button:focus-visible, .cw-voice-button:focus-visible, .cw-send-button:focus-visible,
.cw-lead-input:focus-visible, .cw-lead-checkbox:focus-visible, .cw-lead-submit:focus-visible,
.cw-sched-slot:focus-visible, .cw-sched-checkbox:focus-visible, .cw-sched-confirm-button:focus-visible,
.cw-sched-back-button:focus-visible, .cw-sched-retry:focus-visible, .cw-status-retry:focus-visible,
.cw-sched-handoff-link-button:focus-visible, .cw-sched-handoff-continue-button:focus-visible,
.cw-sched-close:focus-visible, .cw-sched-change-button:focus-visible, .cw-sched-timezone-inline:focus-visible {
  outline: 2px solid var(--cw-ink);
  outline-offset: 2px;
}
/* On ink surfaces the ink focus ring has no contrast — swap to citron there. */
.cw-panel-header :focus-visible {
  outline: 2px solid var(--cw-citron);
  outline-offset: 2px;
}

/* Teaser bubble beside the launcher */
.cw-teaser {
  position: fixed;
  right: 88px;
  bottom: 29px;
  z-index: 2147482999;
  max-width: calc(100vw - 172px);
  padding: 9px 13px;
  border: 1px solid var(--cw-line);
  border-radius: 12px;
  background: var(--cw-paper);
  color: var(--cw-text-secondary);
  box-shadow: 0 6px 18px rgba(25, 26, 23, 0.12);
  font-size: 12px;
  white-space: nowrap;
}
.cw-teaser-tail { position: absolute; right: -5px; bottom: 10px; width: 10px; height: 10px; background: var(--cw-paper); border-top: 1px solid var(--cw-line); border-right: 1px solid var(--cw-line); transform: rotate(45deg); }

.cw-diagnostic { position: fixed; right: 20px; bottom: 88px; z-index: 2147483000; max-width: 320px; padding: 12px 14px; border: 1px solid #b23a32; border-radius: 10px; background: var(--cw-danger-bg); color: var(--cw-danger-ink); font-size: 12px; box-shadow: 0 8px 22px rgba(25, 26, 23, 0.18); }

/* Full-height desktop drawer — 400px max width, flush to the right edge. */
.cw-panel {
  position: fixed;
  top: 24px;
  right: 24px;
  bottom: 24px;
  z-index: 2147483000;
  width: min(412px, calc(100vw - 32px));
  display: flex;
  flex-direction: column;
  overflow: hidden;
  border: 1px solid var(--cw-line);
  border-radius: 18px;
  background: var(--cw-paper);
  box-shadow: 0 30px 70px -24px rgba(15, 23, 42, .4);
  animation: cw-panel-in 220ms cubic-bezier(.16, 1, .3, 1);
}

/* Header — ink bg, avatar gradient circle, name 13.5/700, status 10.5, mute pill, close */
.cw-panel-header { flex: 0 0 auto; display: flex; align-items: center; gap: 12px; min-height: 67px; padding: 16px 12px 16px 18px; border-bottom: 1px solid #eef0f2; background: var(--cw-paper); color: var(--cw-ink); }
.cw-assistant-mark, .cw-welcome-orb {
  display: inline-block;
  position: relative;
  flex: 0 0 auto;
  border-radius: 10px;
  background: conic-gradient(from 45deg, #0ea5e9, #2563eb, #6366f1, #0ea5e9);
  animation: cw-orb-spin 8s linear infinite;
}
.cw-assistant-mark { width: 34px; height: 34px; }
.cw-assistant-mark::after, .cw-welcome-orb::after { content: ""; position: absolute; border-radius: 50%; background: radial-gradient(circle at 35% 30%, #fff, rgba(255,255,255,.35)); }
.cw-assistant-mark::after { inset: 9px; }
.cw-panel-title { display: flex; flex: 1 1 auto; min-width: 0; flex-direction: column; gap: 2px; font-size: 15px; font-weight: 700; line-height: 1.3; }
.cw-panel-role { color: var(--cw-muted); font-size: 12.5px; font-weight: 500; }
.cw-panel-presence { display: flex; align-items: center; gap: 5px; color: var(--cw-success); font-size: 12px; font-weight: 600; }
.cw-panel-presence-offline, .cw-panel-presence-session-expired { color: #b42318; }
.cw-panel-presence-retrying, .cw-panel-presence-reconnecting-session, .cw-panel-presence-rate-limited { color: #8a5a00; }
.cw-presence-dot { width: 6px; height: 6px; border-radius: 50%; background: #22c55e; }
.cw-panel-presence-offline .cw-presence-dot, .cw-panel-presence-session-expired .cw-presence-dot { background: currentColor; }
.cw-panel-presence-retrying .cw-presence-dot, .cw-panel-presence-reconnecting-session .cw-presence-dot, .cw-panel-presence-rate-limited .cw-presence-dot { background: currentColor; }
.cw-header-actions { display: flex; align-items: center; gap: 2px; }

/* Mute "pill" — bordered pill w/ icon + On/Off text, per spec (44px hit target preserved) */
.cw-header-button {
  width: 44px;
  height: 44px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 0;
  border: none;
  border-radius: 8px;
  background: transparent;
  color: var(--cw-muted);
  cursor: pointer;
  transition: background 160ms ease, color 160ms ease;
}
.cw-header-button:hover { background: #f1f5f9; color: var(--cw-ink); }
.cw-header-button:disabled { color: #cbd5e1; cursor: not-allowed; }
.cw-header-button:disabled:hover { background: transparent; }
.cw-header-button svg { width: 17px; height: 17px; }
.cw-close-button svg { width: 18px; height: 18px; }
.cw-mute-toggle svg, .cw-close-button svg, .cw-send-button svg, .cw-launcher svg { display: block; }

/* Offline / connection-status banner — #fff9ec/#f0e2bd/#6a4e00 + bordered "Retry now" pill */
.cw-status { flex: 0 0 auto; display: flex; align-items: center; gap: 8px; padding: 7px 14px; border-bottom: 1px solid var(--cw-warning-line); background: var(--cw-warning-bg); color: var(--cw-warning-ink); font-size: 12px; }
.cw-status:empty { display: none; }
.cw-status-text { flex: 1 1 auto; }
.cw-status-retry { min-height: 44px; padding: 3px 10px; border: 1px solid currentColor; border-radius: 999px; background: transparent; color: inherit; font-size: 11px; font-weight: 600; white-space: nowrap; cursor: pointer; }
.cw-status-retry:hover { background: rgba(106, 78, 0, .08); }

/* Canvas */
.cw-message-list { flex: 1 1 auto; min-height: 0; display: flex; flex-direction: column; gap: 14px; overflow-y: auto; padding: 20px 18px; background: var(--cw-cool-paper); scrollbar-color: #cbd5e1 transparent; }

/* Greeting state */
.cw-welcome { display: flex; flex-direction: column; align-items: center; padding: 8px 0 2px; text-align: center; gap: 11px; }
.cw-welcome-orb { width: 60px; height: 60px; border-radius: 16px; box-shadow: 0 10px 24px -8px rgba(37,99,235,.6); animation-duration: 9s; }
.cw-welcome-orb::after { inset: 14px; }
.cw-welcome h2 { margin: 0; color: var(--cw-ink); font-size: 19px; font-weight: 700; line-height: 1.25; }
.cw-welcome p { max-width: 290px; margin: 0; color: var(--cw-muted); font-size: 14px; line-height: 1.55; }
.cw-suggestions { width: 100%; display: flex; flex-direction: column; gap: 9px; margin-top: 2px; }
.cw-suggestion {
  width: 100%;
  min-height: 44px;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px 14px;
  border: 1px solid var(--cw-line);
  border-radius: 12px;
  background: var(--cw-paper);
  color: var(--cw-text-secondary);
  font-size: 13.5px;
  font-weight: 600;
  text-align: left;
  cursor: pointer;
  transition: border-color 160ms ease, transform 160ms ease, background 160ms ease;
}
.cw-suggestion:hover { border-color: #93c5fd; background: #f0f6ff; }
.cw-suggestion-selected, .cw-suggestion-selected:hover { border: 2px solid #e59500; padding: 11px 13px; background: var(--cw-paper); }
.cw-suggestion:active { transform: scale(.985); }
.cw-suggestion-icon { width: 22px; height: 22px; display: inline-flex; align-items: center; justify-content: center; flex: 0 0 auto; color: var(--cw-citron); }

/* Bubbles — user ink/white 14/14/4/14; bot white/1px border 14/14/14/4 */
.cw-bubble-row { display: flex; width: 100%; animation: cw-message-in 280ms ease-out both; }
.cw-bubble-row-user { justify-content: flex-end; }
.cw-bubble-row-bot { justify-content: flex-start; align-items: flex-end; gap: 9px; }
.cw-bot-mark { position: relative; width: 26px; height: 26px; flex: 0 0 auto; border-radius: 8px; background: conic-gradient(from 45deg,#0ea5e9,#2563eb,#6366f1,#0ea5e9); }
.cw-bot-stack { width: calc(100% - 35px); min-width: 0; display: flex; flex-direction: column; align-items: flex-start; gap: 10px; }
.cw-bot-stack > .cw-bubble-bot { max-width: 100%; }
.cw-bubble { max-width: 82%; padding: 11px 14px; border-radius: 14px; font-size: 14px; line-height: 1.55; overflow-wrap: anywhere; }
.cw-bubble-user { border-radius: 14px 4px 14px 14px; background: var(--cw-citron); color: var(--cw-paper); }
.cw-bubble-bot { max-width: calc(92% - 35px); border: 1px solid var(--cw-line); border-radius: 4px 14px 14px 14px; background: var(--cw-paper); color: var(--cw-text-secondary); }
.cw-md-paragraph { margin: 0; }
.cw-md-paragraph + .cw-md-paragraph { margin-top: 8px; }
.cw-bubble a { color: #1d4ed8; font-weight: 650; text-decoration: underline; }
.cw-bubble code { padding: 1px 4px; border-radius: 4px; background: #ecece5; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; }

/* Human-handoff choice shown before the server-authoritative scheduling or
   lead-capture action. */
.cw-handoff-card {
  width: 100%;
  display: flex;
  flex-direction: column;
  gap: 14px;
  padding: 16px;
  border: 1px solid var(--cw-line);
  border-radius: 16px;
  background: var(--cw-paper);
  box-shadow: 0 10px 24px -20px rgba(15, 23, 42, .42);
}
.cw-handoff-heading { display: flex; align-items: center; gap: 12px; }
.cw-handoff-avatar { width: 44px; height: 44px; flex: 0 0 auto; display: inline-flex; align-items: center; justify-content: center; border-radius: 50%; background: #e2e8f0; color: #52637b; }
.cw-handoff-copy { min-width: 0; display: flex; flex-direction: column; line-height: 1.3; }
.cw-handoff-copy strong { color: var(--cw-ink); font-size: 15px; font-weight: 700; }
.cw-handoff-copy span { color: #8da0bb; font-size: 13px; }
.cw-handoff-actions { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 8px; }
.cw-handoff-talk, .cw-handoff-stay {
  min-height: 48px;
  padding: 10px 16px;
  border-radius: 10px;
  font: inherit;
  font-size: 14px;
  font-weight: 700;
  cursor: pointer;
  transition: border-color .18s ease, background-color .18s ease, color .18s ease, opacity .18s ease;
}
.cw-handoff-talk { border: 1px solid var(--cw-citron); background: var(--cw-citron); color: var(--cw-paper); }
.cw-handoff-stay { border: 1px solid #d8dce2; background: var(--cw-paper); color: #475569; }
.cw-handoff-stay-selected, .cw-handoff-stay-selected:disabled { border: 2px solid #e59500; color: #475569; opacity: 1; }
.cw-handoff-talk:hover:not(:disabled) { background: #1d4ed8; border-color: #1d4ed8; }
.cw-handoff-stay:hover:not(:disabled) { border-color: #e59500; background: #fffbeb; }
.cw-handoff-talk:disabled, .cw-handoff-stay:disabled { cursor: default; opacity: .66; }
.cw-handoff-talk:focus-visible, .cw-handoff-stay:focus-visible { outline: 3px solid var(--cw-focus); outline-offset: 2px; }

.cw-typing { display: flex; gap: 5px; padding: 13px 16px; }
.cw-typing-dot { width: 7px; height: 7px; border-radius: 999px; background: var(--cw-dim); }
.cw-typing-dot:nth-child(2) { background: var(--cw-faint); }
.cw-typing-dot:nth-child(3) { background: var(--cw-line-dashed); }

.cw-line { align-self: center; padding: 7px 10px; border-radius: 8px; font-size: 12px; text-align: center; }
.cw-line-error, .cw-lead-error, .cw-sched-error { border: 1px solid var(--cw-danger-line); background: var(--cw-danger-bg); color: var(--cw-danger-ink); }

/* Composer — pill input + 38px circular citron send (ink arrow); disabled = citron-soft/faint arrow */
.cw-input-row { flex: 0 0 auto; display: flex; flex-direction: column; padding: 12px 16px 15px; border-top: 1px solid #eef0f2; background: var(--cw-paper); }
.cw-composer { display: flex; align-items: center; gap: 6px; min-height: 50px; padding: 6px 6px 6px 14px; border: 1px solid #d8dce2; border-radius: 12px; background: var(--cw-paper); transition: border-color 160ms ease, box-shadow 160ms ease; }
.cw-composer:focus-within { border-color: #93c5fd; box-shadow: 0 0 0 3px rgba(37,99,235,.1); }
.cw-input { flex: 1 1 auto; min-width: 0; min-height: 36px; padding: 6px 0; border: none; outline: none; background: transparent; color: var(--cw-text-secondary); font-size: 14px; }
.cw-input::placeholder { color: var(--cw-faint); }
.cw-input:disabled { color: var(--cw-line-dashed); }
.cw-send-button {
  width: 44px;
  height: 44px;
  min-width: 44px;
  min-height: 44px;
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: none;
  border-radius: 9px;
  background: var(--cw-citron);
  color: var(--cw-paper);
  cursor: pointer;
  transition: transform 160ms ease, background 160ms ease;
}
.cw-voice-button {
  width: 44px;
  height: 44px;
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 0;
  border: none;
  border-radius: 9px;
  background: transparent;
  color: var(--cw-muted);
  cursor: pointer;
  transition: background 160ms ease, color 160ms ease, box-shadow 160ms ease;
}
.cw-voice-button:hover { background: #f1f5f9; color: var(--cw-ink); }
.cw-voice-button-active { background: #fef2f2; color: #dc2626; animation: cw-mic-pulse 1.3s infinite; }
.cw-voice-button svg { width: 17px; height: 17px; }
.cw-send-button svg { width: 16px; height: 16px; }
.cw-send-button:hover:not(:disabled) { background: #1d4ed8; }
.cw-send-button:active:not(:disabled) { transform: scale(.95); }
.cw-send-button:disabled { background: #bfdbfe; color: var(--cw-paper); cursor: not-allowed; }
.cw-disclaimer { margin: 8px 0 0; color: var(--cw-muted); font-size: 11px; line-height: 1.3; text-align: center; }
.cw-privacy-link { color: #1d4ed8; }
.cw-privacy-link:hover { color: #1e40af; }

/* Lead form + scheduler shared shell — card in-canvas, above a dashed divider */
.cw-lead-form, .cw-sched { display: flex; flex-direction: column; gap: 10px; margin-top: 10px; padding: 14px; border: 1px solid var(--cw-line); border-radius: 14px; background: var(--cw-paper); }
.cw-lead-field { display: flex; flex-direction: column; gap: 4px; }
.cw-lead-label, .cw-sched-list-label { color: #5a5b54; font-size: 11px; font-weight: 600; }
.cw-lead-input { min-height: 44px; padding: 8px 10px; border: 1px solid var(--cw-line); border-radius: 9px; background: var(--cw-paper); color: var(--cw-ink); font-size: 13px; }
.cw-lead-input::placeholder { color: var(--cw-faint); }
.cw-lead-input:disabled { background: var(--cw-cool-paper); color: var(--cw-dim); }

/* Consent checkbox — real <input type=checkbox>, styled as a citron-filled
   15px square w/ ink border via accent-color (keeps native check semantics
   and keyboard/AT behavior fully intact — visual only). */
.cw-lead-consent-row, .cw-sched-consent-row { display: flex; align-items: flex-start; gap: 8px; }
.cw-lead-checkbox, .cw-sched-checkbox {
  width: 18px;
  height: 18px;
  flex: 0 0 auto;
  margin: 1px 0 0;
  accent-color: var(--cw-citron);
  border: 1.5px solid var(--cw-ink);
  border-radius: 4px;
  cursor: pointer;
}
.cw-lead-consent-label, .cw-sched-consent-label { color: #5a5b54; font-size: 11px; line-height: 1.45; }
.cw-lead-error, .cw-sched-error { padding: 7px 8px; border-radius: 7px; font-size: 11px; }

.cw-lead-submit, .cw-sched-confirm-button {
  min-height: 44px;
  align-self: stretch;
  text-align: center;
  padding: 10px 15px;
  border: none;
  border-radius: 9px;
  background: var(--cw-citron);
  color: var(--cw-paper);
  font-size: 13px;
  font-weight: 700;
  cursor: pointer;
  transition: background 160ms ease;
}
.cw-sched-retry {
  min-height: 44px;
  align-self: flex-start;
  padding: 9px 15px;
  border: 1px solid var(--cw-ink);
  border-radius: 999px;
  background: var(--cw-ink);
  color: var(--cw-citron);
  font-size: 12px;
  font-weight: 700;
  cursor: pointer;
  transition: background 160ms ease;
}
.cw-lead-submit:hover:not(:disabled), .cw-sched-confirm-button:hover:not(:disabled), .cw-sched-retry:hover { background: #1d4ed8; }
.cw-lead-submit:disabled, .cw-sched-confirm-button:disabled { background: var(--cw-line); color: var(--cw-dim); cursor: not-allowed; }
.cw-lead-confirmation, .cw-sched-confirmation { margin-top: 10px; padding: 10px 0 0; border-top: 1px dashed var(--cw-line); color: var(--cw-success); font-size: 12px; font-weight: 650; }

/* Scheduler — slot rows radius 10; selected state uses ink border + citron-soft + check */
.cw-sched { color: var(--cw-ink); font-size: 12px; }
.cw-sched-list { display: flex; flex-direction: column; gap: 7px; margin: 0; padding: 0; list-style: none; }
.cw-sched-slot {
  width: 100%;
  min-height: 44px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 9px 12px;
  border: 1px solid var(--cw-line);
  border-radius: 10px;
  background: var(--cw-paper);
  color: var(--cw-ink);
  font-size: 12.5px;
  text-align: left;
  cursor: pointer;
  transition: border-color 160ms ease, background 160ms ease;
}
.cw-sched-slot:hover { border-color: #93c5fd; background: #f0f6ff; }
.cw-sched-empty { color: #5a5b54; }
.cw-sched-confirm-heading { color: var(--cw-ink); font-size: 13px; font-weight: 700; }
.cw-lead-confirmation:focus-visible, .cw-sched-confirmation:focus-visible, .cw-sched-confirm-heading:focus-visible { outline: 2px solid var(--cw-ink); outline-offset: 3px; }
.cw-sched-confirm-actions { display: flex; gap: 8px; }
.cw-sched-back-button {
  min-height: 44px;
  padding: 9px 15px;
  border: 1px solid var(--cw-line);
  border-radius: 999px;
  background: var(--cw-paper);
  color: var(--cw-text-secondary);
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  transition: background 160ms ease;
}
.cw-sched-back-button:hover:not(:disabled) { background: var(--cw-cool-paper); }
.cw-sched-back-button:disabled { color: var(--cw-dim); cursor: not-allowed; }
.cw-connect-sales-button { width: calc(100% - 36px); margin: 8px 18px 0; min-height: 42px; border: 1px solid #bfdbfe; border-radius: 10px; background: #eff6ff; color: #1d4ed8; font: inherit; font-size: 13px; font-weight: 700; cursor: pointer; transition: background 160ms ease, border-color 160ms ease; }
.cw-connect-sales-button:hover:not(:disabled) { border-color: #93c5fd; background: #dbeafe; }
.cw-connect-sales-button:disabled { opacity: .55; cursor: not-allowed; }
.cw-sched-month-nav { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.cw-sched-month-nav .cw-sched-back-button { padding: 4px 10px; }
.cw-sched-month-label { font-size: 12px; font-weight: 700; color: var(--cw-ink); }
.cw-sched-weekday-row { display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); gap: 4px; }
.cw-sched-weekday { text-align: center; font-size: 10px; font-weight: 700; color: var(--cw-dim); text-transform: uppercase; }
.cw-sched-calendar { display: flex; flex-direction: column; gap: 4px; }

/* Calendly hosted handoff (SR-6 decision 1) — the compact pre-handoff email
   step + the link-out button. Reuses existing tokens/consent-note styling;
   the link-out button is a REAL focusable <button> (never an anchor with a
   fake target) with an accessible "opens in a new tab" label. */
.cw-sched-handoff { color: var(--cw-ink); font-size: 12px; display: flex; flex-direction: column; gap: 8px; }
.cw-sched-handoff-consent-note { color: #5a5b54; font-size: 11px; line-height: 1.45; margin: 0; }
.cw-sched-handoff-continue-button, .cw-sched-handoff-link-button {
  min-height: 44px;
  align-self: stretch;
  text-align: center;
  padding: 10px 15px;
  border: 1px solid var(--cw-ink);
  border-radius: 999px;
  font-size: 13px;
  font-weight: 700;
  cursor: pointer;
  transition: background 160ms ease;
}
.cw-sched-handoff-continue-button {
  border: none;
  background: var(--cw-ink);
  color: var(--cw-citron);
}
.cw-sched-handoff-continue-button:hover:not(:disabled) { background: #30312c; }
.cw-sched-handoff-continue-button:disabled { background: var(--cw-line); color: var(--cw-dim); cursor: not-allowed; }
.cw-sched-handoff-link-button {
  background: var(--cw-citron);
  color: var(--cw-ink);
}
.cw-sched-handoff-link-button:hover { background: var(--cw-citron-soft); }
.cw-sched-week-row { display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); gap: 4px; }
.cw-sched-week-row [role="gridcell"] { display: flex; }
.cw-sched-day-blank { flex: 1; }
.cw-sched-day { flex: 1; padding: 6px 0; min-width: 0; }
.cw-sched-calendar .cw-sched-slot:disabled { color: var(--cw-dim); background: var(--cw-cool-paper); cursor: not-allowed; }
.cw-sched-tz-label { color: #5a5b54; font-size: 11px; font-weight: 600; }
.cw-sched-tz-select { width: 100%; }
.cw-sched-recap { display: flex; flex-direction: column; gap: 4px; padding: 8px 10px; border-radius: 8px; background: var(--cw-cool-paper); font-size: 12px; }
.cw-sched-recap-label { color: var(--cw-dim); font-weight: 600; margin-right: 4px; }

/* Rebecca scheduling flow — staged card matching the supplied date, time,
   invite, and success states while preserving server-driven availability. */
.cw-sched-card {
  width: 100%;
  max-width: 100%;
  gap: 16px;
  margin-top: 0;
  padding: 18px;
  border-radius: 18px;
  box-shadow: 0 12px 28px -18px rgba(15, 23, 42, .36);
}
.cw-sched-card-header { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.cw-sched-card-header h3 { margin: 0; color: var(--cw-ink); font-size: 18px; line-height: 1.25; font-weight: 700; }
.cw-sched-close {
  width: 44px;
  height: 44px;
  margin: -10px -10px -10px 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 0;
  border-radius: 9px;
  background: transparent;
  color: #8da0bb;
  cursor: pointer;
  transition: background 160ms ease, color 160ms ease;
}
.cw-sched-close:hover { background: #f1f5f9; color: var(--cw-ink); }
.cw-sched-rep { display: flex; align-items: center; gap: 12px; color: var(--cw-ink); }
.cw-sched-rep-avatar { width: 40px; height: 40px; flex: 0 0 auto; border-radius: 50%; background: #dbe5f0; }
.cw-sched-rep > span:last-child { display: flex; flex-direction: column; line-height: 1.25; }
.cw-sched-rep-label { color: #8da0bb; font-size: 12px; font-weight: 500; }
.cw-sched-rep strong { font-size: 15px; font-weight: 700; }
.cw-sched-card .cw-sched-month-nav { margin-top: 2px; }
.cw-sched-card .cw-sched-month-label { font-size: 15px; }
.cw-sched-month-actions { display: inline-flex; gap: 2px; }
.cw-sched-month-actions .cw-sched-back-button { width: 44px; min-height: 44px; padding: 0; border: 0; background: transparent; color: #47617f; font-size: 20px; }
.cw-sched-month-actions .cw-sched-back-button:disabled { color: #cbd5e1; }
.cw-sched-day-strip { width: 100%; overflow-x: auto; padding-bottom: 2px; scrollbar-width: thin; }
.cw-sched-day-strip-row { width: max-content; min-width: 100%; display: grid; grid-auto-flow: column; grid-auto-columns: minmax(45px, 1fr); gap: 5px; }
.cw-sched-day-strip [role="gridcell"] { display: flex; }
.cw-sched-day-strip .cw-sched-day {
  width: 100%;
  min-height: 74px;
  padding: 9px 4px;
  flex-direction: column;
  justify-content: center;
  gap: 4px;
  border-radius: 10px;
  text-align: center;
}
.cw-sched-day-weekday { color: #8798b1; font-size: 10px; font-weight: 700; text-transform: uppercase; }
.cw-sched-day-number { color: var(--cw-ink); font-size: 16px; font-weight: 700; }
.cw-sched-day-strip .cw-sched-day:hover:not(:disabled) { border-color: var(--cw-citron); background: #eff6ff; }
.cw-sched-day-strip .cw-sched-day:focus-visible { border: 2px solid var(--cw-citron); background: #eff6ff; }
.cw-sched-card .cw-sched-tz-label { margin-bottom: -10px; color: #61758f; font-size: 12px; }
.cw-sched-card .cw-sched-tz-select { min-height: 44px; padding: 8px 11px; border: 1px solid var(--cw-line); border-radius: 9px; font-size: 13px; }
.cw-sched-time-heading { display: flex; align-items: center; justify-content: space-between; gap: 10px; color: #61758f; font-size: 13px; font-weight: 600; }
.cw-sched-time-context { min-width: 0; display: flex; align-items: center; gap: 4px; }
.cw-sched-time-context > span:last-child { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.cw-sched-timezone-inline { max-width: 118px; padding: 0; border: 0; background: transparent; color: inherit; font: inherit; font-weight: 650; cursor: pointer; }
.cw-sched-change-button { min-height: 44px; padding: 8px 4px; border: 0; background: transparent; color: #1d4ed8; font-size: 13px; font-weight: 700; cursor: pointer; }
.cw-sched-time-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 7px; }
.cw-sched-time-grid li { min-width: 0; }
.cw-sched-time-slot { justify-content: center; min-height: 48px; padding: 9px 4px; border-radius: 9px; color: #334155; font-size: 13px; font-weight: 650; text-align: center; }
.cw-sched-time-slot-selected { border: 2px solid var(--cw-citron); background: #eff6ff; color: #1d4ed8; }
.cw-sched-time-footer { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.cw-sched-duration { display: inline-flex; align-items: center; gap: 4px; color: #7588a3; font-size: 12px; }
.cw-sched-continue-button { width: auto; min-width: 126px; align-self: auto; }
.cw-sched-card .cw-sched-recap { gap: 10px; padding: 14px; border-radius: 11px; background: #f1f5f9; }
.cw-sched-card .cw-sched-recap > div { display: flex; flex-direction: column; }
.cw-sched-card .cw-sched-recap-label { margin: 0; color: #8da0bb; font-size: 11px; }
.cw-sched-card .cw-sched-recap strong { color: var(--cw-ink); font-size: 14px; }
.cw-sched-email-label { color: var(--cw-ink); font-size: 14px; font-weight: 700; }
.cw-sched-card .cw-sched-email-input, .cw-sched-card .cw-sched-name-input { min-height: 50px; padding: 11px 13px; border: 1px solid #d8dce2; border-radius: 10px; background: var(--cw-paper); font-size: 14px; }
.cw-sched-card .cw-sched-confirm-actions { align-items: stretch; }
.cw-sched-card .cw-sched-confirm-actions .cw-sched-back-button { flex: 0 0 auto; min-width: 82px; border-radius: 9px; font-size: 13px; }
.cw-sched-card .cw-sched-confirm-actions .cw-sched-confirm-button { flex: 1 1 auto; border-radius: 9px; font-size: 13px; }
.cw-sched-booked-stack { width: 100%; display: flex; flex-direction: column; gap: 10px; }
.cw-sched-booked-message { padding: 11px 14px; border: 1px solid var(--cw-line); border-radius: 4px 14px 14px 14px; background: var(--cw-paper); color: var(--cw-text-secondary); font-size: 14px; line-height: 1.5; }
.cw-sched-success-card { display: flex; align-items: flex-start; gap: 11px; margin: 0; padding: 13px 14px; border: 1px solid #a7e9bd; border-radius: 12px; background: #ecfdf3; color: #087a39; font-size: 13px; line-height: 1.45; }
.cw-sched-success-icon { width: 30px; height: 30px; flex: 0 0 auto; display: inline-flex; align-items: center; justify-content: center; border-radius: 50%; background: #22c55e; color: var(--cw-paper); }
.cw-sched-success-card strong { font-weight: 700; }

@media (prefers-reduced-motion: no-preference) {
  .cw-typing-dot { animation: cw-typing-bounce 1.2s infinite ease-in-out; }
  .cw-typing-dot:nth-child(2) { animation-delay: .15s; }
  .cw-typing-dot:nth-child(3) { animation-delay: .3s; }
}
@keyframes cw-typing-bounce { 0%, 60%, 100% { transform: translateY(0); opacity: .55; } 30% { transform: translateY(-4px); opacity: 1; } }
@keyframes cw-panel-in { from { transform: translateY(8px); opacity: 0; } to { transform: translateY(0); opacity: 1; } }
@keyframes cw-message-in { from { transform: translateY(8px); opacity: 0; } to { transform: translateY(0); opacity: 1; } }
@keyframes cw-launcher-pop { from { transform: scale(.6); opacity: 0; } to { transform: scale(1); opacity: 1; } }
@keyframes cw-orb-spin { to { transform: rotate(360deg); } }
@keyframes cw-mic-pulse { 0%, 100% { box-shadow: 0 0 0 0 rgba(220,38,38,.35); } 50% { box-shadow: 0 0 0 7px rgba(220,38,38,0); } }
@media (prefers-reduced-motion: reduce) {
  .cw-panel, .cw-placeholder, .cw-suggestion, .cw-send-button, .cw-bubble-row,
  .cw-assistant-mark, .cw-welcome-orb, .cw-launcher-orb, .cw-voice-button { animation: none; transition: none; }
}
@media (max-width: 480px) {
  .cw-panel { inset: 0; width: 100vw; height: 100dvh; border-radius: 0; }
  .cw-placeholder { right: 12px; bottom: 12px; width: 56px; min-width: 56px; padding: 0; justify-content: center; }
  .cw-launcher-orb { width: 26px; height: 26px; }
  .cw-launcher-label { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); border: 0; }
  .cw-teaser { right: 80px; bottom: 20px; max-width: calc(100vw - 160px); overflow: hidden; text-overflow: ellipsis; }
}
`;

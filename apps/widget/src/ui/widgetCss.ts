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
  --cw-ink: #191a17;
  --cw-citron: #e4f222;
  --cw-citron-soft: #eef7a8;
  --cw-paper: #ffffff;
  --cw-cool-paper: #f7f7f3;
  --cw-line: #e7e7e2;
  --cw-line-dashed: #d5d5cb;
  --cw-text-secondary: #45463f;
  --cw-muted: #70716a;
  --cw-dim: #96978e;
  --cw-faint: #a8a99f;
  --cw-success: #1f6a2f;
  --cw-success-bg: #dcefdc;
  --cw-warning-bg: #fff9ec;
  --cw-warning-line: #f0e2bd;
  --cw-warning-ink: #6a4e00;
  --cw-danger-ink: #79221d;
  --cw-danger-bg: #fff1ef;
  --cw-danger-line: #d99b95;
  --cw-online: #c9e86a;
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
  right: 20px;
  bottom: 20px;
  z-index: 2147483000;
  width: auto;
  height: 56px;
  min-width: 56px;
  min-height: 56px;
  display: inline-flex;
  align-items: center;
  justify-content: flex-start;
  gap: 9px;
  max-width: calc(100vw - 40px);
  padding: 4px 16px 4px 5px;
  border: none;
  border-radius: 999px;
  background: var(--cw-ink);
  color: var(--cw-paper);
  box-shadow: 0 8px 22px rgba(25, 26, 23, 0.3);
  font-size: 14px;
  font-weight: 700;
  letter-spacing: -0.01em;
  cursor: pointer;
  transition: transform 180ms ease, box-shadow 180ms ease, background 180ms ease;
}
.cw-placeholder:hover { background: #30312c; box-shadow: 0 11px 26px rgba(25, 26, 23, 0.36); }
.cw-placeholder:active { transform: scale(0.97); }
.cw-launcher-orb {
  width: 46px;
  height: 46px;
  flex: 0 0 auto;
  border-radius: 999px;
  background: radial-gradient(circle at 35% 30%, #f4fa9a, #e4f222 70%, #b8c410);
  box-shadow: inset 0 0 0 1px rgba(255, 255, 255, .15);
}
/* Desktop uses an icon-and-text pill. The label collapses visually only on
   narrow screens, while the button's accessible name remains unchanged. */
.cw-launcher-label {
  min-width: 0;
  overflow: hidden;
  color: var(--cw-paper);
  font-size: 15px;
  font-weight: 700;
  line-height: 1;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.cw-placeholder:focus-visible, .cw-input:focus-visible, .cw-suggestion:focus-visible,
.cw-mute-toggle:focus-visible, .cw-close-button:focus-visible, .cw-send-button:focus-visible,
.cw-lead-input:focus-visible, .cw-lead-checkbox:focus-visible, .cw-lead-submit:focus-visible,
.cw-sched-slot:focus-visible, .cw-sched-checkbox:focus-visible, .cw-sched-confirm-button:focus-visible,
.cw-sched-back-button:focus-visible, .cw-sched-retry:focus-visible, .cw-status-retry:focus-visible,
.cw-sched-handoff-link-button:focus-visible, .cw-sched-handoff-continue-button:focus-visible {
  outline: 2px solid var(--cw-ink);
  outline-offset: 2px;
}
/* On ink surfaces the ink focus ring has no contrast — swap to citron there. */
.cw-panel-header :focus-visible, .cw-mute-toggle:focus-visible, .cw-close-button:focus-visible {
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
  top: 0;
  right: 0;
  bottom: 0;
  z-index: 2147483000;
  width: min(400px, calc(100vw - 32px));
  height: 100dvh;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  border: none;
  border-radius: 16px 0 0 16px;
  background: var(--cw-paper);
  box-shadow: 0 12px 34px rgba(25, 26, 23, 0.18);
  animation: cw-panel-in 220ms cubic-bezier(.16, 1, .3, 1);
}

/* Header — ink bg, avatar gradient circle, name 13.5/700, status 10.5, mute pill, close */
.cw-panel-header { flex: 0 0 auto; display: flex; align-items: center; gap: 10px; min-height: 60px; padding: 10px 12px 10px 16px; background: var(--cw-ink); color: var(--cw-paper); }
.cw-assistant-mark, .cw-welcome-orb {
  display: inline-block;
  flex: 0 0 auto;
  border-radius: 999px;
  background: radial-gradient(circle at 35% 30%, #f4fa9a, #e4f222 70%, #b8c410);
}
.cw-assistant-mark { width: 28px; height: 28px; box-shadow: inset 0 0 0 1px rgba(255, 255, 255, .15); }
.cw-panel-title { display: flex; flex: 1 1 auto; min-width: 0; flex-direction: column; gap: 1px; font-size: 15.5px; font-weight: 700; letter-spacing: -0.01em; }
.cw-panel-presence { color: #9b9c93; font-size: 12px; font-weight: 500; }
.cw-header-actions { display: flex; align-items: center; gap: 6px; }

/* Mute "pill" — bordered pill w/ icon + On/Off text, per spec (44px hit target preserved) */
.cw-mute-toggle {
  min-width: 44px;
  min-height: 44px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 5px;
  padding: 3px 10px;
  border: 1px solid rgba(255, 255, 255, .4);
  border-radius: 999px;
  background: transparent;
  color: #e5e6df;
  font-size: 12px;
  font-weight: 600;
  white-space: nowrap;
  cursor: pointer;
  transition: background 160ms ease, color 160ms ease, border-color 160ms ease;
}
.cw-mute-toggle svg { width: 14px; height: 14px; }
.cw-mute-toggle-label { line-height: 1; }
.cw-mute-toggle:hover { background: rgba(255, 255, 255, .12); border-color: rgba(255, 255, 255, .6); color: var(--cw-citron); }

.cw-close-button { width: 44px; height: 44px; display: inline-flex; align-items: center; justify-content: center; border: 0; border-radius: 999px; background: transparent; color: #9b9c93; cursor: pointer; transition: background 160ms ease, color 160ms ease; }
.cw-close-button:hover { background: rgba(255, 255, 255, .12); color: var(--cw-citron); }
.cw-mute-toggle svg, .cw-close-button svg, .cw-send-button svg, .cw-launcher svg { display: block; }

/* Offline / connection-status banner — #fff9ec/#f0e2bd/#6a4e00 + bordered "Retry now" pill */
.cw-status { flex: 0 0 auto; display: flex; align-items: center; gap: 8px; padding: 7px 14px; border-bottom: 1px solid var(--cw-warning-line); background: var(--cw-warning-bg); color: var(--cw-warning-ink); font-size: 12px; }
.cw-status:empty { display: none; }
.cw-status-text { flex: 1 1 auto; }
.cw-status-retry { min-height: 44px; padding: 3px 10px; border: 1px solid currentColor; border-radius: 999px; background: transparent; color: inherit; font-size: 11px; font-weight: 600; white-space: nowrap; cursor: pointer; }
.cw-status-retry:hover { background: rgba(106, 78, 0, .08); }

/* Canvas */
.cw-message-list { flex: 1 1 auto; min-height: 0; display: flex; flex-direction: column; gap: 10px; overflow-y: auto; padding: 14px; background: var(--cw-cool-paper); scrollbar-color: #c6c7bd transparent; }

/* Greeting state */
.cw-welcome { display: flex; min-height: 100%; flex-direction: column; align-items: center; justify-content: center; padding: 24px 5px; text-align: center; gap: 14px; }
.cw-welcome-orb { width: 56px; height: 56px; box-shadow: 0 8px 20px rgba(184, 196, 16, .22); }
.cw-welcome h2 { margin: 0; color: var(--cw-ink); font-size: 20px; font-weight: 700; line-height: 1.25; letter-spacing: -0.02em; }
.cw-welcome p { max-width: 270px; margin: 4px 0 0; color: var(--cw-muted); font-size: 14px; line-height: 1.5; }
.cw-suggestions { width: 100%; display: flex; flex-direction: column; gap: 8px; }
.cw-suggestion {
  width: 100%;
  min-height: 44px;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 12px;
  border: 1px solid var(--cw-line);
  border-radius: 10px;
  background: var(--cw-paper);
  color: var(--cw-text-secondary);
  font-size: 14px;
  text-align: left;
  cursor: pointer;
  transition: border-color 160ms ease, transform 160ms ease, background 160ms ease;
}
.cw-suggestion:hover { border-color: #c5c6bd; background: #fbfbf8; }
.cw-suggestion:active { transform: scale(.985); }

/* Bubbles — user ink/white 14/14/4/14; bot white/1px border 14/14/14/4 */
.cw-bubble-row { display: flex; width: 100%; }
.cw-bubble-row-user { justify-content: flex-end; }
.cw-bubble-row-bot { justify-content: flex-start; }
.cw-bubble { max-width: 85%; padding: 9px 13px; border-radius: 14px; font-size: 15px; line-height: 1.5; overflow-wrap: anywhere; }
.cw-bubble-user { max-width: 80%; border-radius: 14px 14px 4px 14px; background: var(--cw-ink); color: var(--cw-paper); }
.cw-bubble-bot { border: 1px solid var(--cw-line); border-radius: 14px 14px 14px 4px; background: var(--cw-paper); color: var(--cw-ink); }
.cw-md-paragraph { margin: 0; }
.cw-md-paragraph + .cw-md-paragraph { margin-top: 8px; }
.cw-bubble a { color: #38410b; font-weight: 650; text-decoration: underline; }
.cw-bubble code { padding: 1px 4px; border-radius: 4px; background: #ecece5; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; }

.cw-typing { display: flex; gap: 4px; padding: 12px 14px; }
.cw-typing-dot { width: 6px; height: 6px; border-radius: 999px; background: var(--cw-muted); }
.cw-typing-dot:nth-child(2) { background: var(--cw-faint); }
.cw-typing-dot:nth-child(3) { background: var(--cw-line-dashed); }

.cw-line { align-self: center; padding: 7px 10px; border-radius: 8px; font-size: 12px; text-align: center; }
.cw-line-error, .cw-lead-error, .cw-sched-error { border: 1px solid var(--cw-danger-line); background: var(--cw-danger-bg); color: var(--cw-danger-ink); }

/* Composer — pill input + 38px circular citron send (ink arrow); disabled = citron-soft/faint arrow */
.cw-input-row { flex: 0 0 auto; display: flex; align-items: center; gap: 8px; padding: 12px; border-top: 1px solid var(--cw-line); background: var(--cw-paper); }
.cw-input { flex: 1 1 auto; min-width: 0; min-height: 44px; padding: 10px 14px; border: 1px solid var(--cw-line); border-radius: 999px; background: var(--cw-paper); color: var(--cw-ink); font-size: 15px; }
.cw-input::placeholder { color: var(--cw-faint); }
.cw-input:disabled { background: var(--cw-cool-paper); color: var(--cw-line-dashed); }
.cw-send-button {
  width: 38px;
  height: 38px;
  min-width: 44px;
  min-height: 44px;
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: none;
  border-radius: 999px;
  background: var(--cw-citron);
  color: var(--cw-ink);
  cursor: pointer;
  transition: transform 160ms ease, background 160ms ease;
}
.cw-send-button svg { width: 16px; height: 16px; }
.cw-send-button:hover:not(:disabled) { background: #f0ff45; }
.cw-send-button:active:not(:disabled) { transform: scale(.95); }
.cw-send-button:disabled { background: var(--cw-citron-soft); color: var(--cw-faint); cursor: not-allowed; }

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
  border-radius: 999px;
  background: var(--cw-ink);
  color: var(--cw-citron);
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
.cw-lead-submit:hover:not(:disabled), .cw-sched-confirm-button:hover:not(:disabled), .cw-sched-retry:hover { background: #30312c; }
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
.cw-sched-slot:hover { border-color: var(--cw-ink); background: #fafaef; }
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
.cw-connect-sales-button { width: calc(100% - 28px); margin: 8px 14px 0; min-height: 40px; border: 1px solid var(--cw-ink); border-radius: 10px; background: var(--cw-citron); color: var(--cw-ink); font: inherit; font-size: 13px; font-weight: 700; cursor: pointer; }
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

@media (prefers-reduced-motion: no-preference) {
  .cw-typing-dot { animation: cw-typing-bounce 1.2s infinite ease-in-out; }
  .cw-typing-dot:nth-child(2) { animation-delay: .15s; }
  .cw-typing-dot:nth-child(3) { animation-delay: .3s; }
}
@keyframes cw-typing-bounce { 0%, 60%, 100% { transform: translateY(0); opacity: .55; } 30% { transform: translateY(-4px); opacity: 1; } }
@keyframes cw-panel-in { from { transform: translateX(16px); opacity: 0; } to { transform: translateX(0); opacity: 1; } }
@media (prefers-reduced-motion: reduce) { .cw-panel, .cw-placeholder, .cw-suggestion, .cw-send-button { animation: none; transition: none; } }
@media (max-width: 480px) {
  .cw-panel { inset: 0; width: 100vw; height: 100dvh; border-radius: 0; }
  .cw-placeholder { right: 12px; bottom: 12px; width: 56px; min-width: 56px; padding: 0; justify-content: center; }
  .cw-launcher-orb { width: 46px; height: 46px; }
  .cw-launcher-label { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); border: 0; }
  .cw-teaser { right: 80px; bottom: 20px; max-width: calc(100vw - 160px); overflow: hidden; text-overflow: ellipsis; }
}
`;
